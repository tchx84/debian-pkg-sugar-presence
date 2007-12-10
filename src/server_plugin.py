"""XMPP server plugin for Presence Service"""
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
import base64
import logging
import os
from itertools import izip
from string import hexdigits

# Other libraries
import dbus
import gobject
from telepathy.client import (ConnectionManager, Connection, Channel)
from telepathy.interfaces import (CONN_MGR_INTERFACE, CONN_INTERFACE,
    CHANNEL_INTERFACE_GROUP, CHANNEL_TYPE_CONTACT_LIST)
from telepathy.constants import (HANDLE_TYPE_CONTACT, HANDLE_TYPE_GROUP,
    CONNECTION_STATUS_CONNECTED, CONNECTION_STATUS_DISCONNECTED,
    CHANNEL_GROUP_FLAG_CHANNEL_SPECIFIC_HANDLES,
    CONNECTION_STATUS_REASON_NAME_IN_USE)
import sugar.profile

# Presence Service local modules
import psutils
from telepathy_plugin import TelepathyPlugin



_logger = logging.getLogger('s-p-s.server_plugin')


class ServerPlugin(TelepathyPlugin):
    """Telepathy-python-based presence server interface

    The ServerPlugin instance translates network events from
    Telepathy Python into GObject events.  It provides direct
    python calls to perform the required network operations
    to implement the PresenceService.
    """

    _TP_CONN_MANAGER = 'gabble'
    _PROTOCOL = 'jabber'
    _OBJ_PATH_PREFIX = "/org/freedesktop/Telepathy/Connection/gabble/"

    def __init__(self, registry, owner):
        TelepathyPlugin.__init__(self, registry, owner)

        self._friends_channel = None

    def _ip4_address_changed_cb(self, ip4am, address):
        TelepathyPlugin._ip4_address_changed_cb(self, ip4am, address)

        if address:
            _logger.debug("::: valid IP4 address, conn_status %s" %
                          self._conn_status)
            # this is a no-op if starting would be inappropriate right now
            if self._conn_status != CONNECTION_STATUS_CONNECTED:
                self.start()
        else:
            _logger.debug("::: invalid IP4 address, will disconnect")
            self._stop()

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
            'port': dbus.UInt32(5223),
            'old-ssl': True,
            'ignore-ssl-errors': True,
            }

    def suggest_room_for_activity(self, activity_id):
        """Suggest a room to use to share the given activity.
        """
        # We shouldn't have to do this, but Gabble sometimes finds the IRC
        # transport and goes "that has chatrooms, that'll do nicely". Work
        # around it til Gabble gets better at finding the MUC service.
        return '%s@%s' % (activity_id,
                          self._account['fallback-conference-server'])

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
            if not item.object_path.startswith(self._OBJ_PATH_PREFIX):
                continue
            if item[CONN_INTERFACE].GetProtocol() != self._PROTOCOL:
                continue
            if item[CONN_INTERFACE].GetStatus() == CONNECTION_STATUS_CONNECTED:
                test_handle = item[CONN_INTERFACE].RequestHandles(
                    HANDLE_TYPE_CONTACT, [our_name])[0]
                if item[CONN_INTERFACE].GetSelfHandle() != test_handle:
                    continue
            return item
        return None

    def _could_connect(self):
        return bool(self._ip4am.props.address and
                    TelepathyPlugin._could_connect(self))

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
        # FIXME: just trusting the owner's server for now
        server = self._owner.get_server()
        return (server and len(server) and hostname == server)

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

    def _connected_cb(self):
        if not self._owner.get_registered():
            # we successfully register this account
            self._owner.set_registered(True)

        TelepathyPlugin._connected_cb(self)

        # request Friends group channel
        def friends_channel_requested_cb(friends_chan_path):
            self._friends_channel = Channel(self._conn.service_name,
                    friends_chan_path)

        def error_requesting_friends_channel(e):
            _logger.debug('error requesting friends channel: %r' % e)

        handles = self._conn[CONN_INTERFACE].RequestHandles(HANDLE_TYPE_GROUP,
                ["Friends"])
        self._conn[CONN_INTERFACE].RequestChannel(CHANNEL_TYPE_CONTACT_LIST,
                HANDLE_TYPE_GROUP, handles[0], True,
                reply_handler=friends_channel_requested_cb,
                error_handler=error_requesting_friends_channel)

    def _filter_trusted_server(self, handles):
        """Filter a list of contact handles removing the one which aren't hosted
        on a trusted server.
        This function is used to only accept subscriptions coming from a
        trusted server.

        :Parameters:
            `handles` : iterable over (int or long)
                The contacts' handles to filter

        :Returns: a list of handles
        """
        result = []
        if not handles:
            return result

        identifiers = self._conn[CONN_INTERFACE].InspectHandles(
            HANDLE_TYPE_CONTACT, handles)

        for handle, jid in izip(handles, identifiers):
            user, host = jid.split('@', 1)
            if self._server_is_trusted(host):
                result.append(handle)

        return result

    def _publish_members_changed_cb(self, message, added, removed,
                                    local_pending, remote_pending,
                                    actor, reason):
        TelepathyPlugin._publish_members_changed_cb(self, message,
            added, removed, local_pending, remote_pending, actor,
            reason)

        local_pending = self._filter_trusted_server(local_pending)
        if local_pending:
            # accept all requested subscriptions
            self._publish_channel[CHANNEL_INTERFACE_GROUP].AddMembers(
                    local_pending, '')

        # subscribe to people who've subscribed to us, if necessary
        if self._subscribe_channel is not None:
            added = list(set(added) - self._subscribe_members
                         - self._subscribe_remote_pending)
            added = self._filter_trusted_server(added)
            if added:
                self._subscribe_channel[CHANNEL_INTERFACE_GROUP].AddMembers(
                        added, '')

    def _handle_connection_status_change(self, status, reason):
        """Override TelepathyPlugin implementation to manage connection errors
        due to registration problem. So, if for any reason the registered flag
        was not set in the config file but the account was registered, we don't
        fail to connect (see ticket #2062)."""
        if status == self._conn_status:
            return

        if (status == CONNECTION_STATUS_DISCONNECTED and
            reason == CONNECTION_STATUS_REASON_NAME_IN_USE and
            self._account['register']):
            _logger.debug('This account is already registered. Connect to it')
            self._account['register'] = False
            self._stop()
            self._init_connection()
            return

        TelepathyPlugin._handle_connection_status_change(self, status, reason)

    def _publish_channel_cb(self, channel):
        TelepathyPlugin._publish_channel_cb(self, channel)

        publish_handles, local_pending, remote_pending = \
            self._publish_channel[CHANNEL_INTERFACE_GROUP].GetAllMembers()

        local_pending = self._filter_trusted_server(local_pending)
        if local_pending:
            # accept pending subscriptions
            # FIXME: do this async
            self._publish_channel[CHANNEL_INTERFACE_GROUP].AddMembers(
                local_pending, '')

        self._add_not_subscribed_to_subscribe_channel()

    def _subscribe_channel_cb(self, channel):
        TelepathyPlugin._subscribe_channel_cb(self, channel)

        self._add_not_subscribed_to_subscribe_channel()

    def _add_not_subscribed_to_subscribe_channel(self):
        if self._publish_channel is None or self._subscribe_channel is None:
            return

        publish_handles, local_pending, remote_pending = \
                self._publish_channel[CHANNEL_INTERFACE_GROUP].GetAllMembers()

        # request subscriptions from people subscribed to us if we're
        # not subscribed to them
        not_subscribed = set(publish_handles)
        not_subscribed -= self._subscribe_members
        not_subscribed = self._filter_trusted_server(not_subscribed)
        if not_subscribed:
            self._subscribe_channel[CHANNEL_INTERFACE_GROUP].AddMembers(
                    not_subscribed, '')

    def sync_friends(self, keys):
        if self._friends_channel is None or self._subscribe_channel is None:
            # not ready yet
            return

        config_path = os.path.join(sugar.env.get_profile_path(), 'config')
        profile = sugar.profile.Profile(config_path)

        friends_handles = set()
        friends = set()
        for key in keys:
            try:
               decoded = base64.b64decode(key)
            except TypeError:
                # key is invalid; skip this friend
                _logger.debug('skipping friend with invalid key')
                continue

            id = psutils.pubkey_to_keyid(decoded)
            # this assumes that all our friends are on the same server as us
            jid = '%s@%s' % (id, profile.jabber_server)
            friends.add(jid)

        def error_syncing_friends(e):
            _logger.debug('error syncing friends: %r' % e)

        def friends_group_synced():
            _logger.debug('friends group synced')

        def friends_subscribed():
            _logger.debug('friends subscribed')

        def got_friends_handles(handles):
            friends_handles.update(handles)

            # subscribe friends
            self._subscribe_channel[CHANNEL_INTERFACE_GROUP].AddMembers(
                friends_handles, "New friend",
                reply_handler=friends_subscribed,
                error_handler=error_syncing_friends)

            # add friends to the "Friends" group
            self._friends_channel[CHANNEL_INTERFACE_GROUP].AddMembers(
                friends_handles, "New friend",
                reply_handler=friends_group_synced,
                error_handler=error_syncing_friends)

        self._conn[CONN_INTERFACE].RequestHandles(
            HANDLE_TYPE_CONTACT, friends,
            reply_handler=got_friends_handles,
            error_handler=error_syncing_friends)
