#!/usr/bin/env python
# Copyright 2012, Julius Seporaitis
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.


__author__ = "Julius Seporaitis"
__email__ = "julius@seporaitis.net"
__copyright__ = "Copyright 2012, Julius Seporaitis"
__license__ = "Apache 2.0"
__version__ = "1.0.1"


import urllib2
import urlparse
import time
import hashlib
import hmac
import json
import sys
import ConfigParser

import yum
import yum.config
import yum.Errors
import yum.plugins

from os.path import expanduser
from yum.yumRepo import YumRepository


__all__ = ['requires_api_version', 'plugin_type', 'CONDUIT',
           'config_hook', 'postreposetup_hook']

requires_api_version = '2.5'
plugin_type = yum.plugins.TYPE_CORE
CONDUIT = None


def config_hook(conduit):
    yum.config.RepoConf.s3_enabled = yum.config.BoolOption(False)


def postreposetup_hook(conduit):
    """Plugin initialization hook. Setup the S3 repositories."""

    repos = conduit.getRepos()

    for repo in repos.listEnabled():
        if isinstance(repo, YumRepository) and repo.s3_enabled:
            new_repo = S3Repository(repo.id, repo.baseurl)
            new_repo.name = repo.name
            # new_repo.baseurl = repo.baseurl
            new_repo.mirrorlist = repo.mirrorlist
            new_repo.basecachedir = repo.basecachedir
            new_repo.gpgcheck = repo.gpgcheck
            new_repo.gpgkey = repo.gpgkey
            new_repo.proxy = repo.proxy
            new_repo.enablegroups = repo.enablegroups
            if hasattr(repo, 'priority'):
                new_repo.priority = repo.priority
            if hasattr(repo, 'base_persistdir'):
                new_repo.base_persistdir = repo.base_persistdir
            if hasattr(repo, 'metadata_expire'):
                new_repo.metadata_expire = repo.metadata_expire

            repos.delete(repo.id)
            repos.add(new_repo)


class S3Repository(YumRepository):
    """Repository object for Amazon S3, using IAM Roles."""

    def __init__(self, repoid, baseurl):
        super(S3Repository, self).__init__(repoid)
        self.iamrole = None
        self.baseurl = baseurl
        self.grabber = None
        self.enable()

    @property
    def grabfunc(self):
        raise NotImplementedError("grabfunc called, when it shouldn't be!")

    @property
    def grab(self):
        if not self.grabber:
            self.grabber = S3Grabber(self)
            self.grabber.get_role()
            self.grabber.get_credentials()
        return self.grabber


class S3Grabber(object):

    def __init__(self, repo):
        """Initialize file grabber.
        Note: currently supports only single repo.baseurl. So in case of a list
              only the first item will be used.
        """
        if isinstance(repo, basestring):
            self.baseurl = repo
        else:
            if len(repo.baseurl) != 1:
                raise yum.plugins.PluginYumExit("s3iam: repository '{0}' "
                                                "must have only one "
                                                "'baseurl' value" % repo.id)
            else:
                self.baseurl = repo.baseurl[0]
        # Ensure urljoin doesn't ignore base path:
        if not self.baseurl.endswith('/'):
            self.baseurl += '/'

    def get_role(self):
        """Read IAM role from AWS metadata store."""
        # Skip retrieval of iam role if awscli credentials exists
        if self.get_awscli_credentials():
            return

        request = urllib2.Request(
            urlparse.urljoin(
                "http://169.254.169.254",
                "/latest/meta-data/iam/security-credentials/"
            ))

        response = None
        try:
            response = urllib2.urlopen(request)
            self.iamrole = (response.read())
        except:
            print "Error: Access to S3 Yum Repo via IAM Role Denied"
            sys.exit(1)
        finally:
            if response:
                response.close()

    def get_home_path(self):
        return expanduser("~")

    def get_awscli_credentials(self):
        try:
            config = ConfigParser.ConfigParser()
            config.read("%s/.aws/config" % self.get_home_path())
            self.access_key = config.get("default", "aws_access_key_id")
            self.secret_key = config.get("default", "aws_secret_access_key")
            self.token = None
            if self.access_key and self.secret_key:
                return True
        except:
            pass
        return False

    """
    Expected flow:
        1) If you have awscli keys it will use that first
            Note: This assumes the keys are correct and if authorisation fails, you'll get an error
        2) If there are no keys, it will default to iam roles
            Note: If the iam role doesn't have access to the s3 bucket, it'll skip the s3yum repo
    """
    def get_credentials(self):
        """Read IAM credentials from AWS metadata store.
        Note: This method should be explicitly called after constructing new
              object, as in 'explicit is better than implicit'.
        """
        # # Skip retrieval of iam role based credentials if awscli credentials exists
        if self.get_awscli_credentials():
            return

        request = urllib2.Request(
            urlparse.urljoin(
                urlparse.urljoin(
                    "http://169.254.169.254/",
                    "latest/meta-data/iam/security-credentials/",
                ), self.iamrole))

        response = None
        try:
            response = urllib2.urlopen(request)
            data = json.loads(response.read())
        finally:
            if response:
                response.close()

        self.access_key = data['AccessKeyId']
        self.secret_key = data['SecretAccessKey']
        self.token = data['Token']

    def _request(self, path):
        url = urlparse.urljoin(self.baseurl, urllib2.quote(path))
        request = urllib2.Request(url)
        if self.token is not None:
            request.add_header('x-amz-security-token', self.token)
        signature = self.sign(request)
        request.add_header('Authorization', "AWS {0}:{1}".format(
            self.access_key,
            signature))
        return request

    def urlgrab(self, url, filename=None, **kwargs):
        """urlgrab(url) copy the file to the local filesystem."""
        request = self._request(url)
        if filename is None:
            filename = request.get_selector()
            if filename.startswith('/'):
                filename = filename[1:]

        response = None
        try:
            out = open(filename, 'w+')
            response = urllib2.urlopen(request)
            buff = response.read(8192)
            while buff:
                out.write(buff)
                buff = response.read(8192)
        except:
            print "Error: Access to S3 Yum Repo Denied"
        finally:
            if response:
                response.close()
            out.close()

        return filename

    def urlopen(self, url, **kwargs):
        """urlopen(url) open the remote file and return a file object."""
        return urllib2.urlopen(self._request(url))

    def urlread(self, url, limit=None, **kwargs):
        """urlread(url) return the contents of the file as a string."""
        return urllib2.urlopen(self._request(url)).read()

    def sign(self, request, timeval=None):
        """Attach a valid S3 signature to request.
        request - instance of Request
        """
        date = time.strftime("%a, %d %b %Y %H:%M:%S GMT", timeval or time.gmtime())
        request.add_header('Date', date)
        host = request.get_host()

        # TODO: bucket name finding is ugly, I should find a way to support
        # both naming conventions: http://bucket.s3.amazonaws.com/ and
        # http://s3.amazonaws.com/bucket/
        try:
            pos = host.find(".s3")
            assert pos != -1
            bucket = host[:pos]
        except AssertionError:
            raise yum.plugins.PluginYumExit(
                "s3iam: baseurl hostname should be in format: "
                "'<bucket>.s3<aws-region>.amazonaws.com'; "
                "found '%s'" % host)

        resource = "/%s%s" % (bucket, request.get_selector(), )
        if self.token is not None:
            amz_headers = 'x-amz-security-token:%s\n' % self.token
            sigstring = ("%(method)s\n\n\n%(date)s\n"
                         "%(canon_amzn_headers)s"
                         "%(canon_amzn_resource)s") % ({
                'method': request.get_method(),
                'date': request.headers.get('Date'),
                'canon_amzn_headers': amz_headers,
                'canon_amzn_resource': resource})
        else:
            sigstring = ("%(method)s\n\n\n%(date)s\n"
                         "%(canon_amzn_resource)s") % ({
                'method': request.get_method(),
                'date': request.headers.get('Date'),
                'canon_amzn_resource': resource})
        digest = hmac.new(
            str(self.secret_key),
            str(sigstring),
            hashlib.sha1).digest()
        signature = digest.encode('base64')
        return signature