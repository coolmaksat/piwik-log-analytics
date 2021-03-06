#!/usr/bin/env python
# vim: et sw=4 ts=4:
# -*- coding: utf-8 -*-
#
# Piwik - free/libre analytics platform
#
# @link http://piwik.org
# @license http://www.gnu.org/licenses/gpl-3.0.html GPL v3 or later
# @version $Id$
#
# For more info see: http://piwik.org/log-analytics/ and http://piwik.org/docs/log-analytics-tool-how-to/
#
# Requires Python 2.6 or greater.
#

import base64
import bz2
import ConfigParser
import datetime
import fnmatch
import gzip
import hashlib
import httplib
import inspect
import itertools
import logging
import optparse
import os
import os.path
import Queue
import re
import sys
import threading
import time
import urllib2
import urlparse
import functools
import traceback
import MySQLdb as mdb
from datetime import timedelta

try:
    import json
except ImportError:
    try:
        import simplejson as json
    except ImportError:
        if sys.version_info < (2, 6):
            print >> sys.stderr, 'simplejson (http://pypi.python.org/pypi/simplejson/) is required.'
            sys.exit(1)



##
## Constants.
##

STATIC_EXTENSIONS = set((
    'gif jpg jpeg png bmp ico svg svgz ttf otf eot woff class swf css js xml robots.txt'
).split())

DOWNLOAD_EXTENSIONS = set((
    '7z aac arc arj asf asx avi bin csv deb dmg doc docx exe flv gz gzip hqx '
    'ibooks jar mpg mp2 mp3 mp4 mpeg mov movie msi msp odb odf odg odp '
    'ods odt ogg ogv pdf phps ppt pptx qt qtm ra ram rar rpm sea sit tar tbz '
    'bz2 tbz tgz torrent txt wav wma wmv wpd xls xlsx xml xsd z zip '
    'azw3 epub mobi apk'
).split())

# A good source is: http://phpbb-bots.blogspot.com/
EXCLUDED_USER_AGENTS = (
    'adsbot-google',
    'ask jeeves',
    'baidubot',
    'bot-',
    'bot/',
    'ccooter/',
    'crawl',
    'curl',
    'echoping',
    'exabot',
    'feed',
    'googlebot',
    'ia_archiver',
    'java/',
    'libwww',
    'mediapartners-google',
    'msnbot',
    'netcraftsurvey',
    'panopta',
    'robot',
    'spider',
    'surveybot',
    'twiceler',
    'voilabot',
    'yahoo',
    'yandex',
)

PIWIK_DEFAULT_MAX_ATTEMPTS = 3
PIWIK_DEFAULT_DELAY_AFTER_FAILURE = 10
DEFAULT_SOCKET_TIMEOUT = 300

PIWIK_EXPECTED_IMAGE = base64.b64decode(
    'R0lGODlhAQABAIAAAAAAAAAAACH5BAEAAAAALAAAAAABAAEAAAICRAEAOw=='
)

##
## Formats.
##

class BaseFormatException(Exception): pass

class BaseFormat(object):
    def __init__(self, name):
        self.name = name
        self.regex = None
        self.date_format = '%d/%b/%Y:%H:%M:%S'

    def check_format(self, file):
        line = file.readline()
        file.seek(0)
        return self.check_format_line(line)

    def check_format_line(self, line):
        return False

class JsonFormat(BaseFormat):
    def __init__(self, name):
        super(JsonFormat, self).__init__(name)
        self.json = None
        self.date_format = '%Y-%m-%dT%H:%M:%S'

    def check_format_line(self, line):
        try:
            self.json = json.loads(line)
            return True
        except:
            return False

    def match(self, line):
        try:
            self.json = json.loads(line)
            return self
        except:
            self.json = None
            return None

    def get(self, key):
        # Some ugly patchs ...
        if key == 'generation_time_milli':
            self.json[key] =  int(self.json[key] * 1000)
        # Patch date format ISO 8601
        elif key == 'date':
            tz = self.json[key][19:]
            self.json['timezone'] = tz.replace(':', '')
            self.json[key] = self.json[key][:19]

        try:
            return self.json[key]
        except KeyError:
            raise BaseFormatException()

    def get_all(self,):
        return self.json

    def remove_ignored_groups(self, groups):
        for group in groups:
            del self.json[group]

class RegexFormat(BaseFormat):

    def __init__(self, name, regex, date_format=None):
        super(RegexFormat, self).__init__(name)
        if regex is not None:
            self.regex = re.compile(regex)
        if date_format is not None:
            self.date_format = date_format
        self.matched = None

    def check_format_line(self, line):
        return self.match(line)

    def match(self,line):
        if not self.regex:
            return None
        match_result = self.regex.match(line)
        if match_result:
            self.matched = match_result.groupdict()
        else:
            self.matched = None
        return match_result

    def get(self, key):
        try:
            return self.matched[key]
        except KeyError:
            raise BaseFormatException("Cannot find group '%s'." % key)

    def get_all(self,):
        return self.matched

    def remove_ignored_groups(self, groups):
        for group in groups:
            del self.matched[group]

class W3cExtendedFormat(RegexFormat):

    FIELDS_LINE_PREFIX = '#Fields: '

    fields = {
        'date': '(?P<date>^\d+[-\d+]+',
        'time': '[\d+:]+)[.\d]*?', # TODO should not assume date & time will be together not sure how to fix ATM.
        'cs-uri-stem': '(?P<path>/\S*)',
        'cs-uri-query': '(?P<query_string>\S*)',
        'c-ip': '"?(?P<ip>[\d*.-]*)"?',
        'cs(User-Agent)': '(?P<user_agent>".*?"|\S+)',
        'cs(Referer)': '(?P<referrer>\S+)',
        'sc-status': '(?P<status>\d+)',
        'sc-bytes': '(?P<length>\S+)',
        'cs-host': '(?P<host>\S+)',
        'cs-username': '(?P<userid>\S+)',
        'time-taken': '(?P<generation_time_secs>[.\d]+)'
    }

    def __init__(self):
        super(W3cExtendedFormat, self).__init__('w3c_extended', None, '%Y-%m-%d %H:%M:%S')

    def check_format(self, file):
        self.create_regex(file)

        # if we couldn't create a regex, this file does not follow the W3C extended log file format
        if not self.regex:
            file.seek(0)
            return

        first_line = file.readline()

        file.seek(0)
        return self.check_format_line(first_line)

    def create_regex(self, file):
        fields_line = None
        if config.options.w3c_fields:
            fields_line = config.options.w3c_fields

        # collect all header lines up until the Fields: line
        # if we're reading from stdin, we can't seek, so don't read any more than the Fields line
        header_lines = []
        while fields_line is None:
            line = file.readline().strip()

            if not line:
                continue

            if not line.startswith('#'):
                break

            if line.startswith(W3cExtendedFormat.FIELDS_LINE_PREFIX):
                fields_line = line
            else:
                header_lines.append(line)

        if not fields_line:
            return

        # store the header lines for a later check for IIS
        self.header_lines = header_lines

        # Parse the 'Fields: ' line to create the regex to use
        full_regex = []

        expected_fields = type(self).fields.copy() # turn custom field mapping into field => regex mapping

        # if the --w3c-time-taken-millisecs option is used, make sure the time-taken field is interpreted as milliseconds
        if config.options.w3c_time_taken_in_millisecs:
            expected_fields['time-taken'] = '(?P<generation_time_milli>[\d.]+)'

        for mapped_field_name, field_name in config.options.custom_w3c_fields.iteritems():
            expected_fields[mapped_field_name] = expected_fields[field_name]
            del expected_fields[field_name]

        # add custom field regexes supplied through --w3c-field-regex option
        for field_name, field_regex in config.options.w3c_field_regexes.iteritems():
            expected_fields[field_name] = field_regex

        # Skip the 'Fields: ' prefix.
        fields_line = fields_line[9:].strip()
        for field in re.split('\s+', fields_line):
            try:
                regex = expected_fields[field]
            except KeyError:
                regex = '(?:".*?"|\S+)'
            full_regex.append(regex)
        full_regex = '\s+'.join(full_regex)

        logging.debug("Based on 'Fields:' line, computed regex to be %s", full_regex)

        self.regex = re.compile(full_regex)

    def check_for_iis_option(self):
        if not config.options.w3c_time_taken_in_millisecs and self._is_time_taken_milli() and self._is_iis():
            logging.info("WARNING: IIS log file being parsed without --w3c-time-taken-milli option. IIS"
                         " stores millisecond values in the time-taken field. If your logfile does this, the aforementioned"
                         " option must be used in order to get accurate generation times.")

    def _is_iis(self):
        return len([line for line in self.header_lines if 'internet information services' in line.lower() or 'iis' in line.lower()]) > 0

    def _is_time_taken_milli(self):
        return 'generation_time_milli' not in self.regex.pattern

class IisFormat(W3cExtendedFormat):

    fields = W3cExtendedFormat.fields.copy()
    fields.update({
        'time-taken': '(?P<generation_time_milli>[.\d]+)',
        'sc-win32-status': '(?P<__win32_status>\S+)' # this group is useless for log importing, but capturing it
                                                     # will ensure we always select IIS for the format instead of
                                                     # W3C logs when detecting the format. This way there will be
                                                     # less accidental importing of IIS logs w/o --w3c-time-taken-milli.
    })

    def __init__(self):
        super(IisFormat, self).__init__()

        self.name = 'iis'

class AmazonCloudFrontFormat(W3cExtendedFormat):

    fields = W3cExtendedFormat.fields.copy()
    fields.update({
        'x-event': '(?P<event_action>\S+)',
        'x-sname': '(?P<event_name>\S+)',
        'cs-uri-stem': '(?:rtmp:/)?(?P<path>/\S*)',
        'c-user-agent': '(?P<user_agent>".*?"|\S+)',

        # following are present to match cloudfront instead of W3C when we know it's cloudfront
        'x-edge-location': '(?P<x_edge_location>".*?"|\S+)',
        'x-edge-result-type': '(?P<x_edge_result_type>".*?"|\S+)',
        'x-edge-request-id': '(?P<x_edge_request_id>".*?"|\S+)',
        'x-host-header': '(?P<x_host_header>".*?"|\S+)'
    })

    def __init__(self):
        super(AmazonCloudFrontFormat, self).__init__()

        self.name = 'amazon_cloudfront'

    def get(self, key):
        if key == 'event_category' and 'event_category' not in self.matched:
            return 'cloudfront_rtmp'
        elif key == 'status' and 'status' not in self.matched:
            return '200'
        elif key == 'user_agent':
            user_agent = super(AmazonCloudFrontFormat, self).get(key)
            return urllib2.unquote(user_agent)
        else:
            return super(AmazonCloudFrontFormat, self).get(key)

_HOST_PREFIX = '(?P<host>[\w\-\.]*)(?::\d+)?\s+'
_COMMON_LOG_FORMAT = (
    '(?P<ip>\S+)\s+\S+\s+\S+\s+\[(?P<date>.*?)\s+(?P<timezone>.*?)\]\s+'
    '"\S+\s+(?P<path>.*?)\s+\S+"\s+(?P<status>\S+)\s+(?P<length>\S+)'
)
_NCSA_EXTENDED_LOG_FORMAT = (_COMMON_LOG_FORMAT +
    '\s+"(?P<referrer>.*?)"\s+"(?P<user_agent>.*?)"'
)
_S3_LOG_FORMAT = (
    '\S+\s+(?P<host>\S+)\s+\[(?P<date>.*?)\s+(?P<timezone>.*?)\]\s+(?P<ip>\S+)\s+'
    '\S+\s+\S+\s+\S+\s+\S+\s+"\S+\s+(?P<path>.*?)\s+\S+"\s+(?P<status>\S+)\s+\S+\s+(?P<length>\S+)\s+'
    '\S+\s+\S+\s+\S+\s+"(?P<referrer>.*?)"\s+"(?P<user_agent>.*?)"'
)
_ICECAST2_LOG_FORMAT = ( _NCSA_EXTENDED_LOG_FORMAT +
    '\s+(?P<session_time>\S+)'
)

FORMATS = {
    'common': RegexFormat('common', _COMMON_LOG_FORMAT),
    'common_vhost': RegexFormat('common_vhost', _HOST_PREFIX + _COMMON_LOG_FORMAT),
    'ncsa_extended': RegexFormat('ncsa_extended', _NCSA_EXTENDED_LOG_FORMAT),
    'common_complete': RegexFormat('common_complete', _HOST_PREFIX + _NCSA_EXTENDED_LOG_FORMAT),
    'w3c_extended': W3cExtendedFormat(),
    'amazon_cloudfront': AmazonCloudFrontFormat(),
    'iis': IisFormat(),
    's3': RegexFormat('s3', _S3_LOG_FORMAT),
    'icecast2': RegexFormat('icecast2', _ICECAST2_LOG_FORMAT),
    'nginx_json': JsonFormat('nginx_json'),
}

##
## Code.
##

class Configuration(object):
    """
    Stores all the configuration options by reading sys.argv and parsing,
    if needed, the config.inc.php.

    It has 2 attributes: options and filenames.
    """

    class Error(Exception):
        pass

    def _create_parser(self):
        """
        Initialize and return the OptionParser instance.
        """
        option_parser = optparse.OptionParser(
            usage='Usage: %prog [options] log_file [ log_file [...] ]',
            description="Import HTTP access logs to Piwik. "
                         "log_file is the path to a server access log file (uncompressed, .gz, .bz2, or specify - to read from stdin). "
                         " By default, the script will try to produce clean reports and will exclude bots, static files, discard http error and redirects, etc. This is customizable, see below.",
            epilog="About Piwik Server Log Analytics: http://piwik.org/log-analytics/ "
                   "              Found a bug? Please create a ticket in http://dev.piwik.org/ "
                   "              Please send your suggestions or successful user story to hello@piwik.org "
        )

        option_parser.add_option(
            '--debug', '-d', dest='debug', action='count', default=0,
            help="Enable debug output (specify multiple times for more verbose)",
        )
        option_parser.add_option(
            '--url', dest='piwik_url',
            help="REQUIRED Your Piwik server URL, eg. http://example.com/piwik/ or http://analytics.example.net",
        )
        option_parser.add_option(
            '--dry-run', dest='dry_run',
            action='store_true', default=False,
            help="Perform a trial run with no tracking data being inserted into Piwik",
        )
        option_parser.add_option(
            '--show-progress', dest='show_progress',
            action='store_true', default=os.isatty(sys.stdout.fileno()),
            help="Print a progress report X seconds (default: 1, use --show-progress-delay to override)"
        )
        option_parser.add_option(
            '--show-progress-delay', dest='show_progress_delay',
            type='int', default=1,
            help="Change the default progress delay"
        )
        option_parser.add_option(
            '--add-sites-new-hosts', dest='add_sites_new_hosts',
            action='store_true', default=False,
            help="When a hostname is found in the log file, but not matched to any website "
            "in Piwik, automatically create a new website in Piwik with this hostname to "
            "import the logs"
        )
        option_parser.add_option(
            '--idsite', dest='site_id',
            help= ("When specified, "
                   "data in the specified log files will be tracked for this Piwik site ID."
                   " The script will not auto-detect the website based on the log line hostname (new websites will not be automatically created).")
        )
        option_parser.add_option(
            '--idsite-fallback', dest='site_id_fallback',
            help="Default Piwik site ID to use if the hostname doesn't match any "
            "known Website's URL. New websites will not be automatically created. "
            "                         Used only if --add-sites-new-hosts or --idsite are not set",
        )
        default_config = os.path.abspath(
            os.path.join(os.path.dirname(__file__),
            '../../config/config.ini.php'),
        )
        option_parser.add_option(
            '--config', dest='config_file', default=default_config,
            help=(
                "This is only used when --login and --password is not used. "
                "Piwik will read the configuration file (default: %default) to "
                "fetch the Super User token_auth from the config file. "
            )
        )
        option_parser.add_option(
            '--login', dest='login',
            help="You can manually specify the Piwik Super User login"
        )
        option_parser.add_option(
            '--password', dest='password',
            help="You can manually specify the Piwik Super User password"
        )
        option_parser.add_option(
            '--token-auth', dest='piwik_token_auth',
            help="Piwik Super User token_auth, 32 characters hexadecimal string, found in Piwik > API",
        )

        option_parser.add_option(
            '--hostname', dest='hostnames', action='append', default=[],
            help="Accepted hostname (requests with other hostnames will be excluded). "
            "Can be specified multiple times"
        )
        option_parser.add_option(
            '--exclude-path', dest='excluded_paths', action='append', default=[],
            help="Any URL path matching this exclude-path will not be imported in Piwik. Can be specified multiple times"
        )
        option_parser.add_option(
            '--exclude-path-from', dest='exclude_path_from',
            help="Each line from this file is a path to exclude (see: --exclude-path)"
        )
        option_parser.add_option(
            '--include-path', dest='included_paths', action='append', default=[],
            help="Paths to include. Can be specified multiple times. If not specified, all paths are included."
        )
        option_parser.add_option(
            '--include-path-from', dest='include_path_from',
            help="Each line from this file is a path to include"
        )
        option_parser.add_option(
            '--useragent-exclude', dest='excluded_useragents',
            action='append', default=[],
            help="User agents to exclude (in addition to the standard excluded "
            "user agents). Can be specified multiple times",
        )
        option_parser.add_option(
            '--enable-static', dest='enable_static',
            action='store_true', default=False,
            help="Track static files (images, css, js, ico, ttf, etc.)"
        )
        option_parser.add_option(
            '--enable-bots', dest='enable_bots',
            action='store_true', default=False,
            help="Track bots. All bot visits will have a Custom Variable set with name='Bot' and value='$Bot_user_agent_here$'"
        )
        option_parser.add_option(
            '--enable-http-errors', dest='enable_http_errors',
            action='store_true', default=False,
            help="Track HTTP errors (status code 4xx or 5xx)"
        )
        option_parser.add_option(
            '--enable-http-redirects', dest='enable_http_redirects',
            action='store_true', default=False,
            help="Track HTTP redirects (status code 3xx except 304)"
        )
        option_parser.add_option(
            '--enable-reverse-dns', dest='reverse_dns',
            action='store_true', default=False,
            help="Enable reverse DNS, used to generate the 'Providers' report in Piwik. "
                 "Disabled by default, as it impacts performance"
        )
        option_parser.add_option(
            '--strip-query-string', dest='strip_query_string',
            action='store_true', default=False,
            help="Strip the query string from the URL"
        )
        option_parser.add_option(
            '--query-string-delimiter', dest='query_string_delimiter', default='?',
            help="The query string delimiter (default: %default)"
        )
        option_parser.add_option(
            '--log-format-name', dest='log_format_name', default=None,
            help=("Access log format to detect (supported are: %s). "
                  "When not specified, the log format will be autodetected by trying all supported log formats."
                  % ', '.join(sorted(FORMATS.iterkeys())))
        )
        available_regex_groups = ['date', 'path', 'query_string', 'ip', 'user_agent', 'referrer', 'status',
                                  'length', 'host', 'userid', 'generation_time_milli', 'event_action',
                                  'event_name', 'timezone', 'session_time']
        option_parser.add_option(
            '--log-format-regex', dest='log_format_regex', default=None,
            help="Regular expression used to parse log entries. Regexes must contain named groups for different log fields. "
                 "Recognized fields include: %s. For an example of a supported Regex, see the source code of this file. "
                 "Overrides --log-format-name." % (', '.join(available_regex_groups))
        )
        option_parser.add_option(
            '--log-hostname', dest='log_hostname', default=None,
            help="Force this hostname for a log format that doesn't incldude it. All hits "
            "will seem to came to this host"
        )
        option_parser.add_option(
            '--skip', dest='skip', default=0, type='int',
            help="Skip the n first lines to start parsing/importing data at a given line for the specified log file",
        )
        option_parser.add_option(
            '--recorders', dest='recorders', default=1, type='int',
            help="Number of simultaneous recorders (default: %default). "
            "It should be set to the number of CPU cores in your server. "
            "You can also experiment with higher values which may increase performance until a certain point",
        )
        option_parser.add_option(
            '--recorder-max-payload-size', dest='recorder_max_payload_size', default=200, type='int',
            help="Maximum number of log entries to record in one tracking request (default: %default). "
        )
        option_parser.add_option(
            '--replay-tracking', dest='replay_tracking',
            action='store_true', default=False,
            help="Replay piwik.php requests found in custom logs (only piwik.php requests expected). \nSee http://piwik.org/faq/how-to/faq_17033/"
        )
        option_parser.add_option(
            '--replay-tracking-expected-tracker-file', dest='replay_tracking_expected_tracker_file', default='piwik.php',
            help="The expected suffix for tracking request paths. Only logs whose paths end with this will be imported. Defaults "
            "to 'piwik.php' so only requests to the piwik.php file will be imported."
        )
        option_parser.add_option(
            '--output', dest='output',
            help="Redirect output (stdout and stderr) to the specified file"
        )
        option_parser.add_option(
            '--encoding', dest='encoding', default='utf8',
            help="Log files encoding (default: %default)"
        )
        option_parser.add_option(
            '--disable-bulk-tracking', dest='use_bulk_tracking',
            default=True, action='store_false',
            help="Disables use of bulk tracking so recorders record one hit at a time."
        )
        option_parser.add_option(
            '--debug-force-one-hit-every-Ns', dest='force_one_action_interval', default=False, type='float',
            help="Debug option that will force each recorder to record one hit every N secs."
        )
        option_parser.add_option(
            '--force-lowercase-path', dest='force_lowercase_path', default=False, action='store_true',
            help="Make URL path lowercase so paths with the same letters but different cases are "
                 "treated the same."
        )
        option_parser.add_option(
            '--enable-testmode', dest='enable_testmode', default=False, action='store_true',
            help="If set, it will try to get the token_auth from the piwik_tests directory"
        )
        option_parser.add_option(
            '--download-extensions', dest='download_extensions', default=None,
            help="By default Piwik tracks as Downloads the most popular file extensions. If you set this parameter (format: pdf,doc,...) then files with an extension found in the list will be imported as Downloads, other file extensions downloads will be skipped."
        )
        option_parser.add_option(
            '--w3c-map-field', action='callback', callback=functools.partial(self._set_option_map, 'custom_w3c_fields'), type='string',
            help="Map a custom log entry field in your W3C log to a default one. Use this option to load custom log "
                 "files that use the W3C extended log format such as those from the Advanced Logging W3C module. Used "
                 "as, eg, --w3c-map-field my-date=date. Recognized default fields include: %s\n\n"
                 "Formats that extend the W3C extended log format (like the cloudfront RTMP log format) may define more "
                 "fields that can be mapped."
                     % (', '.join(W3cExtendedFormat.fields.keys()))
        )
        option_parser.add_option(
            '--w3c-time-taken-millisecs', action='store_true', default=False, dest='w3c_time_taken_in_millisecs',
            help="If set, interprets the time-taken W3C log field as a number of milliseconds. This must be set for importing"
                 " IIS logs."
        )
        option_parser.add_option(
            '--w3c-fields', dest='w3c_fields', default=None,
            help="Specify the '#Fields:' line for a log file in the W3C Extended log file format. Use this option if "
                 "your log file doesn't contain the '#Fields:' line which is required for parsing. This option must be used "
                 "in conjuction with --log-format-name=w3c_extended.\n"
                 "Example: --w3c-fields='#Fields: date time c-ip ...'"
        )
        option_parser.add_option(
            '--w3c-field-regex', action='callback', callback=functools.partial(self._set_option_map, 'w3c_field_regexes'), type='string',
            help="Specify a regex for a field in your W3C extended log file. You can use this option to parse fields the "
                 "importer does not natively recognize and then use one of the --regex-group-to-XXX-cvar options to track "
                 "the field in a custom variable. For example, specifying --w3c-field-regex=sc-win32-status=(?P<win32_status>\\S+) "
                 "--regex-group-to-page-cvar=\"win32_status=Windows Status Code\" will track the sc-win32-status IIS field "
                 "in the 'Windows Status Code' custom variable. Regexes must contain a named group."
        )
        option_parser.add_option(
            '--title-category-delimiter', dest='title_category_delimiter', default='/',
            help="If --enable-http-errors is used, errors are shown in the page titles report. If you have "
            "changed General.action_title_category_delimiter in your Piwik configuration, you need to set this "
            "option to the same value in order to get a pretty page titles report."
        )
        option_parser.add_option(
            '--dump-log-regex', dest='dump_log_regex', action='store_true', default=False,
            help="Prints out the regex string used to parse log lines and exists. Can be useful for using formats "
                 "in newer versions of the script in older versions of the script. The output regex can be used with "
                 "the --log-format-regex option."
        )

        option_parser.add_option(
            '--ignore-groups', dest='regex_groups_to_ignore', default=None,
            help="Comma separated list of regex groups to ignore when parsing log lines. Can be used to, for example, "
                 "disable normal user id tracking. See documentation for --log-format-regex for list of available "
                 "regex groups."
        )

        option_parser.add_option(
            '--regex-group-to-visit-cvar', action='callback', callback=functools.partial(self._set_option_map, 'regex_group_to_visit_cvars_map'), type='string',
            help="Track an attribute through a custom variable with visit scope instead of through Piwik's normal "
                 "approach. For example, to track usernames as a custom variable instead of through the uid tracking "
                 "parameter, supply --regex-group-to-visit-cvar=\"userid=User Name\". This will track usernames in a "
                 "custom variable named 'User Name'. See documentation for --log-format-regex for list of available "
                 "regex groups."
        )
        option_parser.add_option(
            '--regex-group-to-page-cvar', action='callback', callback=functools.partial(self._set_option_map, 'regex_group_to_page_cvars_map'), type='string',
            help="Track an attribute through a custom variable with page scope instead of through Piwik's normal "
                 "approach. For example, to track usernames as a custom variable instead of through the uid tracking "
                 "parameter, supply --regex-group-to-page-cvar=\"userid=User Name\". This will track usernames in a "
                 "custom variable named 'User Name'. See documentation for --log-format-regex for list of available "
                 "regex groups."
        )
        option_parser.add_option(
            '--retry-max-attempts', dest='max_attempts', default=PIWIK_DEFAULT_MAX_ATTEMPTS, type='int',
            help="The maximum number of times to retry a failed tracking request."
        )
        option_parser.add_option(
            '--retry-delay', dest='delay_after_failure', default=PIWIK_DEFAULT_DELAY_AFTER_FAILURE, type='int',
            help="The number of seconds to wait before retrying a failed tracking request."
        )
        option_parser.add_option(
            '--request-timeout', dest='request_timeout', default=DEFAULT_SOCKET_TIMEOUT, type='int',
            help="The maximum number of seconds to wait before terminating an HTTP request to Piwik."
        )
        return option_parser

    def _set_option_map(self, option_attr_name, option, opt_str, value, parser):
        """
        Sets a key-value mapping in a dict that is built from command line options. Options that map
        string keys to string values (like --w3c-map-field) can set the callback to a bound partial
        of this method to handle the option.
        """

        parts = value.split('=')

        if len(parts) != 2:
            fatal_error("Invalid %s option: '%s'" % (opt_str, value))

        key, value = parts

        if not hasattr(parser.values, option_attr_name):
            setattr(parser.values, option_attr_name, {})

        getattr(parser.values, option_attr_name)[key] = value

    def _parse_args(self, option_parser):
        """
        Parse the command line args and create self.options and self.filenames.
        """
        self.options, self.filenames = option_parser.parse_args(sys.argv[1:])

        if self.options.output:
            sys.stdout = sys.stderr = open(self.options.output, 'a+', 0)

        if not self.filenames:
            print(option_parser.format_help())
            sys.exit(1)

        # Configure logging before calling logging.{debug,info}.
        logging.basicConfig(
            format='%(asctime)s: [%(levelname)s] %(message)s',
            level=logging.DEBUG if self.options.debug >= 1 else logging.INFO,
        )

        self.options.excluded_useragents = set([s.lower() for s in self.options.excluded_useragents])

        if self.options.exclude_path_from:
            paths = [path.strip() for path in open(self.options.exclude_path_from).readlines()]
            self.options.excluded_paths.extend(path for path in paths if len(path) > 0)
        if self.options.excluded_paths:
            self.options.excluded_paths = set(self.options.excluded_paths)
            logging.debug('Excluded paths: %s', ' '.join(self.options.excluded_paths))

        if self.options.include_path_from:
            paths = [path.strip() for path in open(self.options.include_path_from).readlines()]
            self.options.included_paths.extend(path for path in paths if len(path) > 0)
        if self.options.included_paths:
            self.options.included_paths = set(self.options.included_paths)
            logging.debug('Included paths: %s', ' '.join(self.options.included_paths))

        if self.options.hostnames:
            logging.debug('Accepted hostnames: %s', ', '.join(self.options.hostnames))
        else:
            logging.debug('Accepted hostnames: all')

        if self.options.log_format_regex:
            self.format = RegexFormat('custom', self.options.log_format_regex)
        elif self.options.log_format_name:
            try:
                self.format = FORMATS[self.options.log_format_name]
            except KeyError:
                fatal_error('invalid log format: %s' % self.options.log_format_name)
        else:
            self.format = None

        if not hasattr(self.options, 'custom_w3c_fields'):
            self.options.custom_w3c_fields = {}
        elif self.format is not None:
            # validate custom field mappings
            for custom_name, default_name in self.options.custom_w3c_fields.iteritems():
                if default_name not in type(format).fields:
                    fatal_error("custom W3C field mapping error: don't know how to parse and use the '%' field" % default_name)
                    return

        if not hasattr(self.options, 'regex_group_to_visit_cvars_map'):
            self.options.regex_group_to_visit_cvars_map = {}

        if not hasattr(self.options, 'regex_group_to_page_cvars_map'):
            self.options.regex_group_to_page_cvars_map = {}

        if not hasattr(self.options, 'w3c_field_regexes'):
            self.options.w3c_field_regexes = {}
        else:
            # make sure each custom w3c field regex has a named group
            for field_name, field_regex in self.options.w3c_field_regexes.iteritems():
                if '(?P<' not in field_regex:
                    fatal_error("cannot find named group in custom w3c field regex '%s' for field '%s'" % (field_regex, field_name))
                    return

        if self.options.recorders < 1:
            self.options.recorders = 1

        if self.options.download_extensions:
            self.options.download_extensions = set(self.options.download_extensions.split(','))
        else:
            self.options.download_extensions = DOWNLOAD_EXTENSIONS

        if self.options.regex_groups_to_ignore:
            self.options.regex_groups_to_ignore = set(self.options.regex_groups_to_ignore.split(','))

    def __init__(self):
        self._parse_args(self._create_parser())



class Statistics(object):
    """
    Store statistics about parsed logs and recorded entries.
    Can optionally print statistics on standard output every second.
    """

    class Counter(object):
        """
        Simple integers cannot be used by multithreaded programs. See:
        http://stackoverflow.com/questions/6320107/are-python-ints-thread-safe
        """
        def __init__(self):
            # itertools.count's implementation in C does not release the GIL and
            # therefore is thread-safe.
            self.counter = itertools.count(1)
            self.value = 0

        def increment(self):
            self.value = self.counter.next()

        def advance(self, n):
            for i in range(n):
                self.increment()

        def __str__(self):
            return str(int(self.value))

    def __init__(self):
        self.time_start = None
        self.time_stop = None

        self.piwik_sites = set()                # sites ID
        self.piwik_sites_created = []           # (hostname, site ID)
        self.piwik_sites_ignored = set()        # hostname

        self.count_lines_parsed = self.Counter()
        self.count_lines_recorded = self.Counter()

        # Do not match the regexp.
        self.count_lines_invalid = self.Counter()
        # No site ID found by the resolver.
        self.count_lines_no_site = self.Counter()
        # Hostname filtered by config.options.hostnames
        self.count_lines_hostname_skipped = self.Counter()
        # Static files.
        self.count_lines_static = self.Counter()
        # Ignored user-agents.
        self.count_lines_skipped_user_agent = self.Counter()
        # Ignored HTTP erors.
        self.count_lines_skipped_http_errors = self.Counter()
        # Ignored HTTP redirects.
        self.count_lines_skipped_http_redirects = self.Counter()
        # Downloads
        self.count_lines_downloads = self.Counter()
        # Ignored downloads when --download-extensions is used
        self.count_lines_skipped_downloads = self.Counter()

        # Misc
        self.dates_recorded = set()
        self.monitor_stop = False

    def set_time_start(self):
        self.time_start = time.time()

    def set_time_stop(self):
        self.time_stop = time.time()

    def _compute_speed(self, value, start, end):
        delta_time = end - start
        if value == 0:
            return 0
        if delta_time == 0:
            return 'very high!'
        else:
            return value / delta_time

    def _round_value(self, value, base=100):
        return round(value * base) / base

    def _indent_text(self, lines, level=1):
        """
        Return an indented text. 'lines' can be a list of lines or a single
        line (as a string). One level of indentation is 4 spaces.
        """
        prefix = ' ' * (4 * level)
        if isinstance(lines, basestring):
            return prefix + lines
        else:
            return '\n'.join(
                prefix + line
                for line in lines
            )

    def print_summary(self):
        print '''
Logs import summary
-------------------

    %(count_lines_recorded)d requests imported successfully
    %(count_lines_downloads)d requests were downloads
    %(total_lines_ignored)d requests ignored:
        %(count_lines_skipped_http_errors)d HTTP errors
        %(count_lines_skipped_http_redirects)d HTTP redirects
        %(count_lines_invalid)d invalid log lines
        %(count_lines_no_site)d requests did not match any known site
        %(count_lines_hostname_skipped)d requests did not match any --hostname
        %(count_lines_skipped_user_agent)d requests done by bots, search engines...
        %(count_lines_static)d requests to static resources (css, js, images, ico, ttf...)
        %(count_lines_skipped_downloads)d requests to file downloads did not match any --download-extensions

Website import summary
----------------------

    %(count_lines_recorded)d requests imported to %(total_sites)d sites
        %(total_sites_existing)d sites already existed
        %(total_sites_created)d sites were created:
%(sites_created)s
    %(total_sites_ignored)d distinct hostnames did not match any existing site:
%(sites_ignored)s
%(sites_ignored_tips)s

Performance summary
-------------------

    Total time: %(total_time)d seconds
    Requests imported per second: %(speed_recording)s requests per second

Processing your log data
------------------------

    In order for your logs to be processed by Piwik, you may need to run the following command:
     ./console core:archive --force-all-websites --force-all-periods=315576000 --force-date-last-n=1000 --url='%(url)s'
''' % {

    'count_lines_recorded': self.count_lines_recorded.value,
    'count_lines_downloads': self.count_lines_downloads.value,
    'total_lines_ignored': sum([
            self.count_lines_invalid.value,
            self.count_lines_skipped_user_agent.value,
            self.count_lines_skipped_http_errors.value,
            self.count_lines_skipped_http_redirects.value,
            self.count_lines_static.value,
            self.count_lines_skipped_downloads.value,
            self.count_lines_no_site.value,
            self.count_lines_hostname_skipped.value,
        ]),
    'count_lines_invalid': self.count_lines_invalid.value,
    'count_lines_skipped_user_agent': self.count_lines_skipped_user_agent.value,
    'count_lines_skipped_http_errors': self.count_lines_skipped_http_errors.value,
    'count_lines_skipped_http_redirects': self.count_lines_skipped_http_redirects.value,
    'count_lines_static': self.count_lines_static.value,
    'count_lines_skipped_downloads': self.count_lines_skipped_downloads.value,
    'count_lines_no_site': self.count_lines_no_site.value,
    'count_lines_hostname_skipped': self.count_lines_hostname_skipped.value,
    'total_sites': len(self.piwik_sites),
    'total_sites_existing': len(self.piwik_sites - set(site_id for hostname, site_id in self.piwik_sites_created)),
    'total_sites_created': len(self.piwik_sites_created),
    'sites_created': self._indent_text(
            ['%s (ID: %d)' % (hostname, site_id) for hostname, site_id in self.piwik_sites_created],
            level=3,
        ),
    'total_sites_ignored': len(self.piwik_sites_ignored),
    'sites_ignored': self._indent_text(
            self.piwik_sites_ignored, level=3,
        ),
    'sites_ignored_tips': '''
        TIPs:
         - if one of these hosts is an alias host for one of the websites
           in Piwik, you can add this host as an "Alias URL" in Settings > Websites.
         - use --add-sites-new-hosts if you wish to automatically create
           one website for each of these hosts in Piwik rather than discarding
           these requests.
         - use --idsite-fallback to force all these log lines with a new hostname
           to be recorded in a specific idsite (for example for troubleshooting/visualizing the data)
         - use --idsite to force all lines in the specified log files
           to be all recorded in the specified idsite
         - or you can also manually create a new Website in Piwik with the URL set to this hostname
''' if self.piwik_sites_ignored else '',
    'total_time': self.time_stop - self.time_start,
    'speed_recording': self._round_value(self._compute_speed(
            self.count_lines_recorded.value,
            self.time_start, self.time_stop,
        )),
    'url': config.options.piwik_url
}

    ##
    ## The monitor is a thread that prints a short summary each second.
    ##

    def _monitor(self):
        latest_total_recorded = 0
        while not self.monitor_stop:
            current_total = stats.count_lines_recorded.value
            time_elapsed = time.time() - self.time_start
            print '%d lines parsed, %d lines recorded, %d records/sec (avg), %d records/sec (current)' % (
                stats.count_lines_parsed.value,
                current_total,
                current_total / time_elapsed if time_elapsed != 0 else 0,
                (current_total - latest_total_recorded) / config.options.show_progress_delay,
            )
            latest_total_recorded = current_total
            time.sleep(config.options.show_progress_delay)

    def start_monitor(self):
        t = threading.Thread(target=self._monitor)
        t.daemon = True
        t.start()

    def stop_monitor(self):
        self.monitor_stop = True


class Recorder(object):
    """
    A Recorder fetches hits from the Queue and inserts them into database.
    """

    recorders = []

    db_host = 'localhost'
    db_name = 'icestat'
    db_user = 'root'
    db_pass = ''


    def __init__(self):
        self.queue = Queue.Queue(maxsize=2)

        # if bulk tracking disabled, make sure we can store hits outside of the Queue
        if not config.options.use_bulk_tracking:
            self.unrecorded_hits = []

    @classmethod
    def launch(cls, recorder_count):
        """
        Launch a bunch of Recorder objects in a separate thread.
        """
        for i in xrange(recorder_count):
            recorder = Recorder()
            cls.recorders.append(recorder)

            run = recorder._run_bulk if config.options.use_bulk_tracking else recorder._run_single
            t = threading.Thread(target=run)

            t.daemon = True
            t.start()
            logging.debug('Launched recorder')

    @classmethod
    def add_hits(cls, all_hits):
        """
        Add a set of hits to the recorders queue.
        """
        # Organize hits so that one client IP will always use the same queue.
        # We have to do this so visits from the same IP will be added in the right order.
        hits_by_client = [[] for r in cls.recorders]
        for hit in all_hits:
            hits_by_client[hit.get_visitor_id_hash() % len(cls.recorders)].append(hit)

        for i, recorder in enumerate(cls.recorders):
            recorder.queue.put(hits_by_client[i])

    @classmethod
    def wait_empty(cls):
        """
        Wait until all recorders have an empty queue.
        """
        for recorder in cls.recorders:
            recorder._wait_empty()

    def _run_bulk(self):
        while True:
            hits = self.queue.get()
            if len(hits) > 0:
                try:
                    self._record_hits(hits)
                except Exception, e:
                    fatal_error(e, hits[0].filename, hits[0].lineno) # approximate location of error
            self.queue.task_done()

    def _run_single(self):
        while True:
            if config.options.force_one_action_interval != False:
                time.sleep(config.options.force_one_action_interval)

            if len(self.unrecorded_hits) > 0:
                hit = self.unrecorded_hits.pop(0)

                try:
                    self._record_hits([hit])
                except Exception, e:
                    fatal_error(e, hit.filename, hit.lineno)
            else:
                self.unrecorded_hits = self.queue.get()
                self.queue.task_done()

    def _wait_empty(self):
        """
        Wait until the queue is empty.
        """
        while True:
            if self.queue.empty():
                # We still have to wait for the last queue item being processed
                # (queue.empty() returns True before queue.task_done() is
                # called).
                self.queue.join()
                return
            time.sleep(1)

    def date_to_piwik(self, date):
        date, time = date.isoformat(sep=' ').split()
        return '%s %s' % (date, time.replace('-', ':'))


    def _record_hits(self, hits):
        """
        Inserts several hits into database.
        """
        self.connection = mdb.connect(
            host=self.db_host, user=self.db_user, passwd=self.db_pass,
            db=self.db_name, charset='utf8')

        sql = """
            INSERT INTO statistics_access (ip, filename, is_download, session_time,
                is_redirect, event_category, event_action, lineno, status,
                is_error, event_name, date, session_start_date, path,
                extension, referrer, userid, length, user_agent,
                generation_time_milli, query_string, is_robot, full_path,
                country_code, country, city, latitude, longitude,
                region, region_name, organization)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                %s, %s, %s, %s, %s, %s)
        """
        try:
            c = self.connection.cursor()
            for hit in hits:
                if hit.session_time > 0:
                    hit.session_start_date = hit.date - timedelta(
                        seconds=hit.session_time)
                    user_info = hit.user_agent.split(":")
                    hit.country_code = ''
                    hit.country = ''
                    hit.city = ''
                    hit.latitude = ''
                    hit.longitude = ''
                    hit.region = ''
                    hit.region_name = ''
                    hit.organization = ''
                    if len(user_info) == 1:
                        hit.user_agent = user_info[0]
                    elif len(user_info) == 6:
                        hit.country_code = user_info[0]
                        hit.country = user_info[1]
                        hit.city = user_info[2]
                        hit.latitude = user_info[3]
                        hit.longitude = user_info[4]
                        hit.user_agent = user_info[5]
                    elif len(user_info) == 7:
                        hit.country_code = user_info[0]
                        hit.country = user_info[1]
                        hit.city = user_info[2]
                        hit.latitude = user_info[3]
                        hit.longitude = user_info[4]
                        hit.organization = user_info[5]
                        hit.user_agent = user_info[6]
                    elif len(user_info) == 9:
                        hit.country_code = user_info[0]
                        hit.country = user_info[1]
                        hit.city = user_info[2]
                        hit.latitude = user_info[3]
                        hit.longitude = user_info[4]
                        hit.region = user_info[5]
                        hit.region_name = user_info[6]
                        hit.organization = user_info[7]
                        hit.user_agent = user_info[8]
                    try:
                        c.execute(sql, (hit.ip, hit.filename, hit.is_download,
                                        hit.session_time, hit.is_redirect,
                                        hit.event_category, hit.event_action,
                                        hit.lineno, hit.status, hit.is_error,
                                        hit.event_name, hit.date,
                                        hit.session_start_date, hit.path,
                                        hit.extension, hit.referrer,
                                        hit.userid, hit.length, hit.user_agent,
                                        hit.generation_time_milli,
                                        hit.query_string, hit.is_robot,
                                        hit.full_path, hit.country_code,
                                        hit.country, hit.city, hit.latitude,
                                        hit.longitude, hit.region,
                                        hit.region_name, hit.organization))
                    except Exception, e:
                        print e
        except Exception, e:
            print e
        self.connection.commit()
        self.connection.close()
        stats.count_lines_recorded.advance(len(hits))

    def _is_json(self, result):
        try:
            json.loads(result)
            return True
        except ValueError, e:
            return False

    def _on_tracking_failure(self, response, data):
        """
        Removes the successfully tracked hits from the request payload so
        they are not logged twice.
        """
        try:
            response = json.loads(response)
        except:
            # the response should be in JSON, but in case it can't be parsed just try another attempt
            logging.debug("cannot parse tracker response, should be valid JSON")
            return response

        # remove the successfully tracked hits from payload
        tracked = response['tracked']
        data['requests'] = data['requests'][tracked:]

        return response['message']


class Hit(object):
    """
    It's a simple container.
    """
    def __init__(self, **kwargs):
        for key, value in kwargs.iteritems():
            setattr(self, key, value)
        super(Hit, self).__init__()

        if config.options.force_lowercase_path:
            self.full_path = self.full_path.lower()

    def get_visitor_id_hash(self):
        visitor_id = self.ip

        if config.options.replay_tracking:
            for param_name_to_use in ['uid', 'cid', '_id', 'cip']:
                if param_name_to_use in self.args:
                    visitor_id = self.args[param_name_to_use]
                    break

        return abs(hash(visitor_id))

    def add_page_custom_var(self, key, value):
        """
        Adds a page custom variable to this Hit.
        """
        self._add_custom_var(key, value, 'cvar')

    def add_visit_custom_var(self, key, value):
        """
        Adds a visit custom variable to this Hit.
        """
        self._add_custom_var(key, value, '_cvar')

    def _add_custom_var(self, key, value, api_arg_name):
        if api_arg_name not in self.args:
            self.args[api_arg_name] = {}

        if isinstance(self.args[api_arg_name], basestring):
            logging.debug("Ignoring custom %s variable addition [ %s = %s ], custom var already set to string." % (api_arg_name, key, value))
            return

        index = len(self.args[api_arg_name]) + 1
        self.args[api_arg_name][index] = [key, value]

class Parser(object):
    """
    The Parser parses the lines in a specified file and inserts them into
    a Queue.
    """

    def __init__(self):
        self.check_methods = [method for name, method
                              in inspect.getmembers(self, predicate=inspect.ismethod)
                              if name.startswith('check_')]

    ## All check_* methods are called for each hit and must return True if the
    ## hit can be imported, False otherwise.

    def check_hostname(self, hit):
        # Check against config.hostnames.
        if not hasattr(hit, 'host') or not config.options.hostnames:
            return True

        # Accept the hostname only if it matches one pattern in the list.
        result = any(
            fnmatch.fnmatch(hit.host, pattern)
            for pattern in config.options.hostnames
        )
        if not result:
            stats.count_lines_hostname_skipped.increment()
        return result

    def check_static(self, hit):
        if hit.extension in STATIC_EXTENSIONS:
            if config.options.enable_static:
                hit.is_download = True
                return True
            else:
                stats.count_lines_static.increment()
                return False
        return True

    def check_download(self, hit):
        if hit.extension in config.options.download_extensions:
            stats.count_lines_downloads.increment()
            hit.is_download = True
            return True
        # the file is not in the white-listed downloads
        # if it's a know download file, we shall skip it
        elif hit.extension in DOWNLOAD_EXTENSIONS:
            stats.count_lines_skipped_downloads.increment()
            return False
        return True

    def check_user_agent(self, hit):
        user_agent = hit.user_agent.lower()
        for s in itertools.chain(EXCLUDED_USER_AGENTS, config.options.excluded_useragents):
            if s in user_agent:
                if config.options.enable_bots:
                    hit.is_robot = True
                    return True
                else:
                    stats.count_lines_skipped_user_agent.increment()
                    return False
        return True

    def check_http_error(self, hit):
        if hit.status[0] in ('4', '5'):
            if config.options.replay_tracking:
                # process error logs for replay tracking, since we don't care if piwik error-ed the first time
                return True
            elif config.options.enable_http_errors:
                hit.is_error = True
                return True
            else:
                stats.count_lines_skipped_http_errors.increment()
                return False
        return True

    def check_http_redirect(self, hit):
        if hit.status[0] == '3' and hit.status != '304':
            if config.options.enable_http_redirects:
                hit.is_redirect = True
                return True
            else:
                stats.count_lines_skipped_http_redirects.increment()
                return False
        return True

    def check_path(self, hit):
        for excluded_path in config.options.excluded_paths:
            if fnmatch.fnmatch(hit.path, excluded_path):
                return False
        # By default, all paths are included.
        if config.options.included_paths:
           for included_path in config.options.included_paths:
               if fnmatch.fnmatch(hit.path, included_path):
                   return True
           return False
        return True

    @staticmethod
    def check_format(lineOrFile):
        format = False
        format_groups = 0
        for name, candidate_format in FORMATS.iteritems():
            logging.debug("Check format %s", name)

            match = None
            try:
                if isinstance(lineOrFile, basestring):
                    match = candidate_format.check_format_line(lineOrFile)
                else:
                    match = candidate_format.check_format(lineOrFile)
            except Exception, e:
                logging.debug('Error in format checking: %s', traceback.format_exc())
                pass

            if match:
                logging.debug('Format %s matches', name)

                # compare format groups if this *BaseFormat has groups() method
                try:
                    # if there's more info in this match, use this format
                    match_groups = len(match.groups())

                    logging.debug('Format match contains %d groups' % match_groups)

                    if format_groups < match_groups:
                        format = candidate_format
                        format_groups = match_groups
                except AttributeError:
                    format = candidate_format

            else:
                logging.debug('Format %s does not match', name)

        # if the format is W3cExtendedFormat, check if the logs are from IIS and if so, issue a warning if the
        # --w3c-time-taken-milli option isn't set
        if isinstance(format, W3cExtendedFormat):
            format.check_for_iis_option()

        return format

    @staticmethod
    def detect_format(file):
        """
        Return the best matching format for this file, or None if none was found.
        """
        logging.debug('Detecting the log format')

        format = False

        # check the format using the file (for formats like the W3cExtendedFormat one)
        format = Parser.check_format(file)

        # check the format using the first N lines (to avoid irregular ones)
        lineno = 0
        limit = 100000
        while not format and lineno < limit:
            line = file.readline()
            if not line: # if at eof, don't keep looping
                break

            lineno = lineno + 1

            logging.debug("Detecting format against line %i" % lineno)
            format = Parser.check_format(line)

        try:
            file.seek(0)
        except IOError:
            pass

        if not format:
            fatal_error("cannot automatically determine the log format using the first %d lines of the log file. " % limit +
                        "\nMaybe try specifying the format with the --log-format-name command line argument." )
            return

        logging.debug('Format %s is the best match', format.name)
        return format

    def parse(self, filename):
        """
        Parse the specified filename and insert hits in the queue.
        """
        def invalid_line(line, reason):
            stats.count_lines_invalid.increment()
            if config.options.debug >= 2:
                logging.debug('Invalid line detected (%s): %s' % (reason, line))

        if filename == '-':
            filename = '(stdin)'
            file = sys.stdin
        else:
            if not os.path.exists(filename):
                print >> sys.stderr, "\n=====> Warning: File %s does not exist <=====" % filename
                return
            else:
                if filename.endswith('.bz2'):
                    open_func = bz2.BZ2File
                elif filename.endswith('.gz'):
                    open_func = gzip.open
                else:
                    open_func = open
                file = open_func(filename, 'r')

        if config.options.show_progress:
            print 'Parsing log %s...' % filename

        if config.format:
            # The format was explicitely specified.
            format = config.format

            if isinstance(format, W3cExtendedFormat):
                format.create_regex(file)

                if format.regex is None:
                    return fatal_error(
                        "File is not in the correct format, is there a '#Fields:' line? "
                        "If not, use the --w3c-fields option."
                    )
        else:
            # If the file is empty, don't bother.
            data = file.read(100)
            if len(data.strip()) == 0:
                return
            try:
                file.seek(0)
            except IOError:
                pass

            format = self.detect_format(file)
            if format is None:
                return fatal_error(
                    'Cannot guess the logs format. Please give one using '
                    'either the --log-format-name or --log-format-regex option'
                )
        # Make sure the format is compatible with the resolver.

        if config.options.dump_log_regex:
            logging.info("Using format '%s'." % format.name)
            if format.regex:
                logging.info("Regex being used: %s" % format.regex.pattern)
            else:
                logging.info("Format %s does not use a regex to parse log lines." % format.name)
            logging.info("--dump-log-regex option used, aborting log import.")
            os._exit(0)

        hits = []
        for lineno, line in enumerate(file):
            try:
                line = line.decode(config.options.encoding)
            except UnicodeDecodeError:
                invalid_line(line, 'invalid encoding')
                continue

            stats.count_lines_parsed.increment()
            if stats.count_lines_parsed.value <= config.options.skip:
                continue

            match = format.match(line)
            if not match:
                invalid_line(line, 'line did not match')
                continue

            hit = Hit(
                filename=filename,
                lineno=lineno,
                status=format.get('status'),
                full_path=format.get('path'),
                is_download=False,
                is_robot=False,
                is_error=False,
                is_redirect=False,
                args={},
            )

            if config.options.regex_group_to_page_cvars_map:
                self._add_custom_vars_from_regex_groups(hit, format, config.options.regex_group_to_page_cvars_map, True)

            if config.options.regex_group_to_visit_cvars_map:
                self._add_custom_vars_from_regex_groups(hit, format, config.options.regex_group_to_visit_cvars_map, False)

            if config.options.regex_groups_to_ignore:
                format.remove_ignored_groups(config.options.regex_groups_to_ignore)

            try:
                hit.query_string = format.get('query_string')
                hit.path = hit.full_path
            except BaseFormatException:
                hit.path, _, hit.query_string = hit.full_path.partition(config.options.query_string_delimiter)

            # W3cExtendedFormat detaults to - when there is no query string, but we want empty string
            if hit.query_string == '-':
                hit.query_string = ''

            hit.extension = hit.path.rsplit('.')[-1].lower()

            try:
                hit.referrer = format.get('referrer')

                if hit.referrer.startswith('"'):
                    hit.referrer = hit.referrer[1:-1]
            except BaseFormatException:
                hit.referrer = ''
            if hit.referrer == '-':
                hit.referrer = ''

            try:
                hit.user_agent = format.get('user_agent')

                # in case a format parser included enclosing quotes, remove them so they are not
                # sent to Piwik
                if hit.user_agent.startswith('"'):
                    hit.user_agent = hit.user_agent[1:-1]
            except BaseFormatException:
                hit.user_agent = ''

            hit.ip = format.get('ip')
            try:
                hit.length = int(format.get('length'))
            except (ValueError, BaseFormatException):
                # Some lines or formats don't have a length (e.g. 304 redirects, W3C logs)
                hit.length = 0

            try:
                hit.generation_time_milli = float(format.get('generation_time_milli'))
            except BaseFormatException:
                try:
                    hit.generation_time_milli = float(format.get('generation_time_micro')) / 1000
                except BaseFormatException:
                    try:
                        hit.generation_time_milli = float(format.get('generation_time_secs')) * 1000
                    except BaseFormatException:
                        hit.generation_time_milli = 0

            if config.options.log_hostname:
                hit.host = config.options.log_hostname
            else:
                try:
                    hit.host = format.get('host').lower().strip('.')

                    if hit.host.startswith('"'):
                        hit.host = hit.host[1:-1]
                except BaseFormatException:
                    # Some formats have no host.
                    pass

            # Add userid
            try:
                hit.userid = None

                userid = format.get('userid')
                if userid != '-':
                    hit.args['uid'] = hit.userid = userid
            except:
                pass

            # add event info
            try:
                hit.event_category = hit.event_action = hit.event_name = None

                hit.event_category = format.get('event_category')
                hit.event_action = format.get('event_action')

                hit.event_name = format.get('event_name')
                if hit.event_name == '-':
                    hit.event_name = None
            except:
                pass

            # add session time
            try:
                hit.session_time = None

                session_time = format.get('session_time')
                hit.session_time = int(session_time)
            except:
                pass

            # Check if the hit must be excluded.
            if not all((method(hit) for method in self.check_methods)):
                continue

            # Parse date.
            # We parse it after calling check_methods as it's quite CPU hungry, and
            # we want to avoid that cost for excluded hits.
            date_string = format.get('date')
            try:
                hit.date = datetime.datetime.strptime(date_string, format.date_format)
            except ValueError:
                invalid_line(line, 'invalid date')
                continue

            # Parse timezone and substract its value from the date
            try:
                timezone = float(format.get('timezone'))
            except BaseFormatException:
                timezone = 0
            except ValueError:
                invalid_line(line, 'invalid timezone')
                continue

            if timezone:
                hit.date -= datetime.timedelta(hours=timezone/100)

            if config.options.replay_tracking:
                # we need a query string and we only consider requests with piwik.php
                if not hit.query_string or not hit.path.lower().endswith(config.options.replay_tracking_expected_tracker_file):
                    invalid_line(line, 'no query string, or ' + hit.path.lower() + ' does not end with piwik.php')
                    continue

                query_arguments = urlparse.parse_qs(hit.query_string)
                if not "idsite" in query_arguments:
                    invalid_line(line, 'missing idsite')
                    continue

                try:
                    hit.args.update((k, v.pop().encode('raw_unicode_escape').decode(config.options.encoding)) for k, v in query_arguments.iteritems())
                except UnicodeDecodeError:
                    invalid_line(line, 'invalid encoding')
                    continue

            hits.append(hit)
            if len(hits) >= config.options.recorder_max_payload_size * len(Recorder.recorders):
                Recorder.add_hits(hits)
                hits = []
        if len(hits) > 0:
            Recorder.add_hits(hits)


    def _add_custom_vars_from_regex_groups(self, hit, format, groups, is_page_var):
        for group_name, custom_var_name in groups.iteritems():
            if group_name in format.get_all():
                value = format.get(group_name)

                # don't track the '-' empty placeholder value
                if value == '-':
                    continue

                if is_page_var:
                    hit.add_page_custom_var(custom_var_name, value)
                else:
                    hit.add_visit_custom_var(custom_var_name, value)

def main():
    """
    Start the importing process.
    """
    stats.set_time_start()

    if config.options.show_progress:
        stats.start_monitor()

    recorders = Recorder.launch(config.options.recorders)

    try:
        for filename in config.filenames:
            parser.parse(filename)

        Recorder.wait_empty()
    except KeyboardInterrupt:
        pass

    stats.set_time_stop()

    if config.options.show_progress:
        stats.stop_monitor()

    stats.print_summary()

def fatal_error(error, filename=None, lineno=None):
    print >> sys.stderr, 'Fatal error: %s' % error
    if filename and lineno is not None:
        print >> sys.stderr, (
            'You can restart the import of "%s" from the point it failed by '
            'specifying --skip=%d on the command line.\n' % (filename, lineno)
        )
    os._exit(1)

if __name__ == '__main__':
    try:
        config = Configuration()
        stats = Statistics()
        parser = Parser()
        main()
        sys.exit(0)
    except KeyboardInterrupt:
        pass
