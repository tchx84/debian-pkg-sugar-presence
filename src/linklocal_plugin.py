"""Link-local plugin for Presence Service"""
# Copyright (C) 2007, Red Hat, Inc.
# Copyright (C) 2007, Collabora Ltd.
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 2 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program; if not, write to the Free Software
# Foundation, Inc., 51 Franklin St, Fifth Floor, Boston, MA  02110-1301  USA

# Standard library
import logging
from itertools import izip
from string import hexdigits

# Other libraries
import gobject
from dbus import SystemBus
from telepathy.client import (ConnectionManager, Connection)
from telepathy.interfaces import (CONN_MGR_INTERFACE, CONN_INTERFACE,
    CHANNEL_INTERFACE_GROUP)
from telepathy.constants import (HANDLE_TYPE_CONTACT,
    CONNECTION_STATUS_CONNECTED, CONNECTION_STATUS_DISCONNECTED,
    CHANNEL_GROUP_FLAG_CHANNEL_SPECIFIC_HANDLES)

# Presence Service local modules
import psutils
from telepathy_plugin import TelepathyPlugin


CONN_INTERFACE_BUDDY_INFO = 'org.laptop.Telepathy.BuddyInfo'

_logger = logging.getLogger('s-p-s.linklocal_plugin')


class LinkLocalPlugin(TelepathyPlugin):
    """Telepathy-python-based presence server interface

    The ServerPlugin instance translates network events from
    Telepathy Python into GObject events.  It provides direct
    python calls to perform the required network operations
    to implement the PresenceService.
    """

    _TP_CONN_MANAGER = 'salut'
    _PROTOCOL = 'local-xmpp'
    _OBJ_PATH_PREFIX = "/org/freedesktop/Telepathy/Connection/salut/"

    def __init__(self, registry, owner):
        TelepathyPlugin.__init__(self, registry, owner)

        self._sys_bus = SystemBus()
        self._have_avahi = False
        self._watch = self._sys_bus.watch_name_owner('org.freedesktop.Avahi',
                                                     self._avahi_owner_cb)

    def _avahi_owner_cb(self, unique_name):
        had_avahi = self._have_avahi

        if unique_name:
            self._have_avahi = True
            if not had_avahi:
                if self._backoff_id > 0:
                    _logger.info('Avahi appeared on the system bus (%s) - '
                                 'will start when retry time is reached')
                else:
                    _logger.info('Avahi appeared on the system bus (%s) - '
                                 'starting...', unique_name)
                    self.start()
        else:
            self._have_avahi = False
            if had_avahi:
                _logger.info('Avahi disappeared from the system bus - '
                             'stopping...')
                self._stop()

    def cleanup(self):
        TelepathyPlugin.cleanup(self)
        if self._watch is not None:
            self._watch.cancel()
        self._watch = None

    def _could_connect(self):
        return TelepathyPlugin._could_connect(self) and self._have_avahi

    def _get_account_info(self):
        """Retrieve connection manager parameters for this account
        """
        server = self._owner.get_server()
        khash = psutils.pubkey_to_keyid(self._owner.props.key)

        return {
            'nickname': '%s' % self._owner.props.nick,
            'first-name': ' ',
            'last-name': '%s' % self._owner.props.nick,
            'jid': '%s@%s' % (khash, server),
            'published-name': '%s' % self._owner.props.nick,
            }

    def _find_existing_connection(self):
        """Try to find an existing Telepathy connection to this server

        filters the set of connections from
            telepathy.client.Connection.get_connections
        to find a connection using our protocol with the
        "self handle" of that connection being a handle
        which matches our account (see _get_account_info)

        returns connection or None
        """
        # Search existing connections, if any, that we might be able to use
        connections = Connection.get_connections()
        for item in connections:
            if not item.object_path.startswith(self._OBJ_PATH_PREFIX):
                continue
            if item[CONN_INTERFACE].GetProtocol() != self._PROTOCOL:
                continue
            # Any Salut instance will do
            return item
        return None

    def identify_contacts(self, tp_chan, handles, identifiers=None):
        """Work out the "best" unique identifier we can for the given handles,
        in the context of the given channel (which may be None), using only
        'fast' connection manager API (that does not involve network
        round-trips).

        For the XMPP server case, we proceed as follows:

        * Find the owners of the given handles, if the channel has
          channel-specific handles
        * If the owner (globally-valid JID) is on a trusted server, return
          'keyid/' plus the 'key fingerprint' (the user part of their JID,
          currently implemented as the SHA-1 of the Base64 blob in
          owner.key.pub)
        * If the owner (globally-valid JID) cannot be found or is on an
          untrusted server, return 'xmpp/' plus an escaped form of the JID

        The idea is that we identify buddies by key-ID (i.e. by key, assuming
        no collisions) if we can find it without making network round-trips,
        but if that's not possible we just use their JIDs.

        :Parameters:
            `tp_chan` : telepathy.client.Channel or None
                The channel in which the handles were found, or None if they
                are known to be channel-specific handles
            `handles` : iterable over (int or long)
                The contacts' handles in that channel
        :Returns:
            A dict mapping the provided handles to the best available
            unique identifier, which is a string that could be used as a
            suffix to an object path
        """
        # we need to be able to index into handles, so force them to
        # be a sequence
        if not isinstance(handles, (tuple, list)):
            handles = tuple(handles)

        # we happen to know that Salut has no channel-specific handles

        if identifiers is None:
            identifiers = self._conn[CONN_INTERFACE].InspectHandles(
                HANDLE_TYPE_CONTACT, handles)

        ret = {}
        for handle, ident in izip(handles, identifiers):
            # special-case the Owner - we always know who we are
            if handle == self.self_handle:
                ret[handle] = self._owner.props.objid
                continue

            # we also happen to know that on Salut, getting properties
            # is immediate, and the key is (well, will be) trustworthy

            if CONN_INTERFACE_BUDDY_INFO in self._conn:
                props = self._conn[CONN_INTERFACE_BUDDY_INFO].GetProperties(
                    handle, byte_arrays=True, utf8_strings=True)
                key = props.get('key')
            else:
                key = None

            if key is not None:
                khash = psutils.pubkey_to_keyid(key)
                ret[handle] = 'keyid/' + khash
            else:
                ret[handle] = 'salut/' + psutils.escape_identifier(ident)

        return ret
