"""Telepathy-python presence server interface/implementation plugin"""
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
import os
import sys
from itertools import izip
from string import hexdigits

# Other libraries
import dbus
import gobject
import gtk
from telepathy.client import (ConnectionManager, ManagerRegistry, Connection,
    Channel)
from telepathy.interfaces import (CONN_MGR_INTERFACE, CONN_INTERFACE,
    CHANNEL_TYPE_CONTACT_LIST, CHANNEL_INTERFACE_GROUP,
    CONN_INTERFACE_ALIASING, CONN_INTERFACE_AVATARS, CONN_INTERFACE_PRESENCE,
    CHANNEL_TYPE_TEXT, CHANNEL_TYPE_STREAMED_MEDIA, PROPERTIES_INTERFACE)
from telepathy.constants import (HANDLE_TYPE_CONTACT,
    HANDLE_TYPE_LIST, HANDLE_TYPE_CONTACT, HANDLE_TYPE_ROOM,
    CONNECTION_STATUS_CONNECTED, CONNECTION_STATUS_DISCONNECTED,
    CONNECTION_STATUS_CONNECTING,
    CONNECTION_STATUS_REASON_AUTHENTICATION_FAILED,
    CONNECTION_STATUS_REASON_NONE_SPECIFIED,
    CHANNEL_GROUP_FLAG_CHANNEL_SPECIFIC_HANDLES,
    PROPERTY_FLAG_WRITE)
from sugar import util

# Presence Service local modules
import psutils
from telepathy_plugin import TelepathyPlugin


CONN_INTERFACE_BUDDY_INFO = 'org.laptop.Telepathy.BuddyInfo'
CONN_INTERFACE_ACTIVITY_PROPERTIES = 'org.laptop.Telepathy.ActivityProperties'

_PROTOCOL = "jabber"
_OBJ_PATH_PREFIX = "/org/freedesktop/Telepathy/Connection/gabble/jabber/"

_logger = logging.getLogger('s-p-s.server_plugin')


class ServerPlugin(TelepathyPlugin):
    """Telepathy-python-based presence server interface

    The ServerPlugin instance translates network events from
    Telepathy Python into GObject events.  It provides direct
    python calls to perform the required network operations
    to implement the PresenceService.
    """

    def __init__(self, registry, owner):
        TelepathyPlugin.__init__(self, registry, owner)

        # Monitor IPv4 address as an indicator of the network connection
        self._ip4am = psutils.IP4AddressMonitor.get_instance()
        self._ip4am.connect('address-changed', self._ip4_address_changed_cb)

    def _ip4_address_changed_cb(self, ip4am, address):
        _logger.debug("::: IP4 address now %s", address)
        if address:
            _logger.debug("::: valid IP4 address, conn_status %s",
                          self._conn_status)
            if self._conn_status == CONNECTION_STATUS_DISCONNECTED:
                _logger.debug("::: will connect")
                self.start()
        else:
            _logger.debug("::: invalid IP4 address, will disconnect")
            self.cleanup()

    def _get_account_info(self):
        """Retrieve connection manager parameters for this account
        """
        server = self._owner.get_server()
        khash = psutils.pubkey_to_keyid(self._owner.props.key)

        return {
            'account': "%s@%s" % (khash, server),
            'fallback-conference-server': "conference.%s" % server,
            'password': self._owner.get_key_hash(),
            'register': not self._owner.get_registered(),
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
        our_name = self._account['account']

        # Search existing connections, if any, that we might be able to use
        connections = Connection.get_connections()
        for item in connections:
            if not item.object_path.startswith(_OBJ_PATH_PREFIX):
                continue
            if item[CONN_INTERFACE].GetProtocol() != _PROTOCOL:
                continue
            if item[CONN_INTERFACE].GetStatus() == CONNECTION_STATUS_CONNECTED:
                test_handle = item[CONN_INTERFACE].RequestHandles(
                    HANDLE_TYPE_CONTACT, [our_name])[0]
                if item[CONN_INTERFACE].GetSelfHandle() != test_handle:
                    continue
            return item
        return None

    def _init_connection(self):
        """Set up our connection

        if there is no existing connection
            (_find_existing_connection returns None)
        produce a new connection with our protocol for our
        account.

        if there is an existing connection, reuse it by
        registering for various of events on it.
        """
        conn = self._find_existing_connection()
        if not conn:
            acct = self._account.copy()

            # Create a new connection
            gabble_mgr = self._registry.GetManager('gabble')
            name, path = gabble_mgr[CONN_MGR_INTERFACE].RequestConnection(
                _PROTOCOL, acct)
            conn = Connection(name, path)
            del acct

        m = conn[CONN_INTERFACE].connect_to_signal('StatusChanged',
           self._handle_connection_status_change)
        self._matches.append(m)
        m = conn[CONN_INTERFACE].connect_to_signal('NewChannel',
                                                   self._new_channel_cb)
        self._matches.append(m)

        # hack
        conn._valid_interfaces.add(CONN_INTERFACE_PRESENCE)
        conn._valid_interfaces.add(CONN_INTERFACE_BUDDY_INFO)
        conn._valid_interfaces.add(CONN_INTERFACE_ACTIVITY_PROPERTIES)
        conn._valid_interfaces.add(CONN_INTERFACE_AVATARS)
        conn._valid_interfaces.add(CONN_INTERFACE_ALIASING)

        m = conn[CONN_INTERFACE_PRESENCE].connect_to_signal('PresenceUpdate',
            self._presence_update_cb)
        self._matches.append(m)

        self._conn = conn
        status = self._conn[CONN_INTERFACE].GetStatus()

        if status == CONNECTION_STATUS_DISCONNECTED:
            def connect_reply():
                _logger.debug('Connect() succeeded')
            def connect_error(e):
                _logger.debug('Connect() failed: %s', e)
                if not self._reconnect_id:
                    self._reconnect_id = gobject.timeout_add(self._RECONNECT_TIMEOUT,
                            self._reconnect_cb)

            self._conn[CONN_INTERFACE].Connect(reply_handler=connect_reply,
                                               error_handler=connect_error)

        self._handle_connection_status_change(status,
                CONNECTION_STATUS_REASON_NONE_SPECIFIED)

    def _connected_cb(self):
        """Callback on successful connection to a server
        """

        if self._account['register']:
            # we successfully register this account
            self._owner.set_registered(True)

        # request both handles at the same time to reduce round-trips
        pub_handle, sub_handle = self._conn[CONN_INTERFACE].RequestHandles(
                HANDLE_TYPE_LIST, ['publish', 'subscribe'])

        # the group of contacts who may receive your presence
        publish = self._conn.request_channel(CHANNEL_TYPE_CONTACT_LIST,
                HANDLE_TYPE_LIST, pub_handle, True)
        self._publish_channel = publish
        m = publish[CHANNEL_INTERFACE_GROUP].connect_to_signal(
                'MembersChanged', self._publish_members_changed_cb)
        self._matches.append(m)
        publish_handles, local_pending, remote_pending = \
                publish[CHANNEL_INTERFACE_GROUP].GetAllMembers()

        # the group of contacts for whom you wish to receive presence
        subscribe = self._conn.request_channel(CHANNEL_TYPE_CONTACT_LIST,
                HANDLE_TYPE_LIST, sub_handle, True)
        self._subscribe_channel = subscribe
        m = subscribe[CHANNEL_INTERFACE_GROUP].connect_to_signal(
                'MembersChanged', self._subscribe_members_changed_cb)
        self._matches.append(m)
        subscribe_handles, subscribe_lp, subscribe_rp = \
                subscribe[CHANNEL_INTERFACE_GROUP].GetAllMembers()
        self._subscribe_members = set(subscribe_handles)
        self._subscribe_local_pending = set(subscribe_lp)
        self._subscribe_remote_pending = set(subscribe_rp)

        if local_pending:
            # accept pending subscriptions
            # FIXME: do this async
            publish[CHANNEL_INTERFACE_GROUP].AddMembers(local_pending, '')

        # FIXME: do this async?
        self.self_handle = self._conn[CONN_INTERFACE].GetSelfHandle()
        self.self_identifier = self._conn[CONN_INTERFACE].InspectHandles(
                HANDLE_TYPE_CONTACT, [self.self_handle])[0]

        # request subscriptions from people subscribed to us if we're not
        # subscribed to them
        not_subscribed = list(set(publish_handles) - set(subscribe_handles))
        subscribe[CHANNEL_INTERFACE_GROUP].AddMembers(not_subscribed, '')

        if CONN_INTERFACE_BUDDY_INFO not in self._conn.get_valid_interfaces():
            _logger.debug('OLPC information not available')
            return False

        # Request presence for everyone we're subscribed to
        self._conn[CONN_INTERFACE_PRESENCE].RequestPresence(subscribe_handles)
        return True

    def _should_reconnect(self):
        return bool(self._ip4am.props.address)

    def start(self):
        """Start up the Telepathy networking connections

        if we are already connected, query for the initial contact
        information.

        if we are already connecting, do nothing

        otherwise initiate a connection and transfer control to
            _connect_reply_cb or _connect_error_cb
        """

        _logger.debug("Starting up...")

        if self._reconnect_id > 0:
            gobject.source_remove(self._reconnect_id)
            self._reconnect_id = 0

        # Only init connection if we have a valid IP address
        if self._ip4am.props.address:
            _logger.debug("::: Have IP4 address %s, will connect",
                          self._ip4am.props.address)
            self._init_connection()
        else:
            _logger.debug("::: No IP4 address, postponing connection")

    def _server_is_trusted(self, hostname):
        """Return True if the server with the given hostname is trusted to
        verify public-key ownership correctly, and only allows users to
        register JIDs whose username part is either a public key fingerprint,
        or of the wrong form to be a public key fingerprint (to allow for
        ejabberd's admin@example.com address).

        If we trust the server, we can skip verifying the key ourselves,
        which leads to simplifications. In the current implementation we
        never verify that people actually own the key they claim to, so
        we will always give contacts on untrusted servers a JID- rather than
        key-based identity.

        For the moment we assume that the test server, olpc.collabora.co.uk,
        does this verification.
        """
        return (hostname == 'olpc.collabora.co.uk')

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

        owners = handles

        if tp_chan is not None and CHANNEL_INTERFACE_GROUP in tp_chan:
            group = tp_chan[CHANNEL_INTERFACE_GROUP]
            if (group.GetGroupFlags() &
                CHANNEL_GROUP_FLAG_CHANNEL_SPECIFIC_HANDLES):
                identifiers = None
                owners = group.GetHandleOwners(handles)
                for i, owner in enumerate(owners):
                    if owner == 0:
                        owners[i] = handles[i]
        else:
            group = None

        if identifiers is None:
            identifiers = self._conn[CONN_INTERFACE].InspectHandles(
                HANDLE_TYPE_CONTACT, owners)

        ret = {}
        for handle, jid in izip(handles, identifiers):
            # special-case the Owner - we always know who we are
            if (handle == self.self_handle or
                (group is not None and handle == group.GetSelfHandle())):
                ret[handle] = self._owner.props.objid
                continue

            if '/' in jid:
                # the contact is unidentifiable (in an anonymous MUC) - create
                # a temporary identity for them, based on their room-JID
                ret[handle] = 'xmpp/' + psutils.escape_identifier(jid)
            else:
                user, host = jid.split('@', 1)
                if (self._server_is_trusted(host) and len(user) == 40 and
                    user.strip(hexdigits) == ''):
                    # they're on a trusted server and their username looks
                    # like a key-ID
                    ret[handle] = 'keyid/' + user.lower()
                else:
                    # untrusted server, or not the right format to be a
                    # key-ID - identify the contact by their JID
                    ret[handle] = 'xmpp/' + psutils.escape_identifier(jid)

        return ret
