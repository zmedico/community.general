# Based on local.py (c) 2012, Michael DeHaan <michael.dehaan@gmail.com>
# Based on chroot.py (c) 2013, Maykel Moya <mmoya@speedyrails.com>
# Based on func.py
# (c) 2014, Michael Scherer <misc@zarb.org>
# (c) 2017 Ansible Project
# GNU General Public License v3.0+ (see COPYING or https://www.gnu.org/licenses/gpl-3.0.txt)

from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

DOCUMENTATION = '''
    author: Michael Scherer (@mscherer) <misc@zarb.org>
    connection: saltapi
    short_description: Allow ansible to piggyback on salt minions
    description:
        - This allows you to use existing Saltstack infrastructure to connect to targets.
    options:
      plugin:
        description: The name of this plugin.
        choices: ['saltapi', 'community.general.saltapi']
        default: saltapi
        type: str
      url:
        description: URL of salt master.
        required: yes
        type: str
        vars:
            - name: url
            - name: saltapi_url
      token:
        description: Value for X-Auth-Token header.
        required: yes
        type: str
        vars:
            - name: token
            - name: saltapi_token
            - name: ansible_password
      validate_certs:
        description: Verify SSL certificate if using HTTPS.
        type: boolean
        default: true
        vars:
            - name: saltapi_validate_certs
'''

import re
import os
import pty
import base64
import codecs
import sys
import subprocess
from distutils.version import LooseVersion

from ansible.module_utils._text import to_bytes, to_text
from ansible.module_utils.six.moves import cPickle

import os
from ansible import errors
from ansible.plugins.connection import ConnectionBase

# 3rd party imports
try:
    import requests
    if LooseVersion(requests.__version__) < LooseVersion('1.1.0'):
        raise ImportError
    HAS_REQUESTS = True
except ImportError:
    HAS_REQUESTS = False


class Connection(ConnectionBase):
    ''' Salt-based connections '''

    has_pipelining = False
    # while the name of the product is salt, naming that module salt cause
    # trouble with module import
    transport = 'community.general.saltapi'

    def __init__(self, play_context, new_stdin, *args, **kwargs):
        super(Connection, self).__init__(play_context, new_stdin, *args, **kwargs)
        self.host = self._play_context.remote_addr
        self._session = None

    def _get_session(self):
        if not HAS_REQUESTS:
            raise AnsibleError('This module requires Python Requests 1.1.0 or higher: '
                               'https://github.com/psf/requests.')
        if not self._session:
            self._session = requests.session()
            self._session.verify = self.get_option('validate_certs')
            self._session.headers.update({'X-Auth-Token': self.get_option('token')})
        return self._session

    def _connect(self):
        self._get_session()
        self._connected = True
        return self

    def exec_command(self, cmd, sudoable=False, in_data=None):
        ''' run a command on the remote minion '''
        super(Connection, self).exec_command(cmd, in_data=in_data, sudoable=sudoable)

        if in_data:
            raise errors.AnsibleError("Internal Error: this module does not support optimized module pipelining")

        self._display.vvv("EXEC %s" % (cmd), host=self.host)

        json_body = [{
            'client': 'local',
            'tgt': self.host,
            'fun': 'cmd.exec_code_all',
            'arg': ["bash", cmd],
        }]
        resp = self._session.post(self.get_option('url'), json=json_body)

        p = resp.json()['return'][0][self.host]
        if p['retcode'] != 0:
            print('exec_command retcode != 0:', cmd, '\n', p, file=sys.stderr)
        return (p['retcode'], p['stdout'], p['stderr'])

    def _normalize_path(self, path, prefix):
        if not path.startswith(os.path.sep):
            path = os.path.join(os.path.sep, path)
        normpath = os.path.normpath(path)
        return os.path.join(prefix, normpath[1:])

    def put_file(self, in_path, out_path):
        ''' transfer a file from local to remote '''

        super(Connection, self).put_file(in_path, out_path)

        out_path = self._normalize_path(out_path, '/')
        self._display.vvv("PUT %s TO %s" % (in_path, out_path), host=self.host)

        with open(in_path, 'rb') as in_fh:
            content = base64.b64encode(in_fh.read()).decode('utf8')

        json_body = [{
            'client': 'local',
            'tgt': self.host,
            'fun': 'hashutil.base64_decodefile',
            'arg': [content, out_path],
        }]
        resp = self._session.post(self.get_option('url'), json=json_body)
        try:
            resp.raise_for_status()
        except requests.exceptions.HTTPError as http_error:
            msg = "failed to transfer file from %s to %s: %s" % (in_path, out_path, str(http_error))
            raise errors.AnsibleError(msg)

    def fetch_file(self, in_path, out_path):
        ''' fetch a file from remote to local '''

        super(Connection, self).fetch_file(in_path, out_path)

        in_path = self._normalize_path(in_path, '/')
        self._display.vvv("FETCH %s TO %s" % (in_path, out_path), host=self.host)
        retcode, stdout, stderr = self.exec_command('base64 -w0 < "{0}"'.format())

        if retcode != 0:
            msg = "failed to transfer file from %s to %s: %s" % (in_path, out_path, stderr or stdout)
            raise errors.AnsibleError(msg)

    def close(self):
        ''' terminate the connection; nothing to do here '''
        pass
