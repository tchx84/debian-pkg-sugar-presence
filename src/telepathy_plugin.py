"""Base class for Telepathy plugins."""

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

import logging
from itertools import izip

from dbus import DBusException, SessionBus
import gobject

from telepathy.client import (Channel, Connection)
from telepathy.constants import (CONNECTION_STATUS_DISCONNECTED,
    CONNECTION_STATUS_CONNECTING, CONNECTION_STATUS_CONNECTED,
    CONNECTION_STATUS_REASON_AUTHENTICATION_FAILED,
    CONNECTION_STATUS_REASON_NONE_SPECIFIED,
    CHANNEL_GROUP_CHANGE_REASON_INVITED,
    HANDLE_TYPE_CONTACT, HANDLE_TYPE_ROOM, HANDLE_TYPE_LIST)
from telepathy.interfaces import (CONN_INTERFACE, CHANNEL_TYPE_TEXT,
        CHANNEL_TYPE_STREAMED_MEDIA, CHANNEL_INTERFACE_GROUP,
        CONN_INTERFACE_PRESENCE, CONN_INTERFACE_AVATARS,
        CONN_INTERFACE_ALIASING, CHANNEL_TYPE_CONTACT_LIST,
        CONN_MGR_INTERFACE)

import psutils

CONN_INTERFACE_BUDDY_INFO = 'org.laptop.Telepathy.BuddyInfo'
CONN_INTERFACE_ACTIVITY_PROPERTIES = 'org.laptop.Telepathy.ActivityProperties'


_logger = logging.getLogger('s-p-s.telepathy_plugin')


class TelepathyPlugin(gobject.GObject):
    __gsignals__ = {
        'contacts-online':
            # Contacts in the subscribe list have come online.
            # args:
            #   contact identification (based on key ID or JID): list of str
            #   contact handle: list of int or long
            #   contact identifier (JID): list of str or unicode
            (gobject.SIGNAL_RUN_FIRST, None, [object, object, object]),
        'contacts-offline':
            # Contacts in the subscribe list have gone offline.
            # args: iterable over contact handles
            (gobject.SIGNAL_RUN_FIRST, None, [object]),
        'status':
            # Connection status changed.
            # args: status, reason as for Telepathy StatusChanged
            (gobject.SIGNAL_RUN_FIRST, None, [int, int]),
        'activity-invitation':
            # We were invited to join an activity
            # args:
            #   activity room: Channel
            #   activity room handle: int or long
            #   inviter contact handle: int or long
            #   message: unicode
            (gobject.SIGNAL_RUN_FIRST, None, [object] * 4),
        'private-invitation':
            # We were invited to join a chat or a media call
            # args:
            #   channel object path
            (gobject.SIGNAL_RUN_FIRST, None, [object, str]),
        'want-to-connect':
            # The TelepathyPlugin wants to connect. presenceservice.py will
            # call the start() method if that's OK with its policy.
            (gobject.SIGNAL_RUN_FIRST, None, []),
    }

    _RECONNECT_INITIAL_TIMEOUT = 5000    # 5 seconds
    _RECONNECT_MAX_TIMEOUT = 300000      # 5 minutes
    _TP_CONN_MANAGER = 'gabble'
    _PROTOCOL = 'jabber'

    def __init__(self, registry, owner):
        """Initialize the ServerPlugin instance

        :Parameters:
            `registry` : telepathy.client.ManagerRegistry
                From the PresenceService, used to find the connection
                manager details
            `owner` : buddy.GenericOwner
                The Buddy representing the owner of this XO (normally a
                buddy.ShellOwner instance)
        """
        gobject.GObject.__init__(self)

        #: The connection, a `telepathy.client.Connection`
        self._conn = None

        #: List of dbus-python SignalMatch objects representing signal match
        #: rules associated with the connection, so we don't leak the match
        #: rules when disconnected.
        self._matches = []

        #: The manager registry, a `telepathy.client.ManagerRegistry`
        self._registry = registry

        #: set of contact handles: those for whom we've emitted contacts-online
        self._online_contacts = set()

        #: The owner, a `buddy.GenericOwner`
        self._owner = owner
        #: The owner's handle on this connection
        self.self_handle = None
        #: The owner's identifier (e.g. JID) on this connection
        self.self_identifier = None

        #: The connection's status
        self._conn_status = CONNECTION_STATUS_DISCONNECTED

        #: GLib source ID indicating when we may try to reconnect
        self._backoff_id = 0

        #: length of the next reconnect timeout
        self._reconnect_timeout = self._RECONNECT_INITIAL_TIMEOUT

        #: Parameters for the connection manager
        self._account = self._get_account_info()

        #: The ``subscribe`` channel: a `telepathy.client.Channel` or None
        self._subscribe_channel = None
        #: The members of the ``subscribe`` channel
        self._subscribe_members = set()
        #: The local-pending members of the ``subscribe`` channel
        self._subscribe_local_pending = set()
        #: The remote-pending members of the ``subscribe`` channel
        self._subscribe_remote_pending = set()

        #: The ``publish`` channel: a `telepathy.client.Channel` or None
        self._publish_channel = None

        #: Watch the connection on DBus session bus
        self._session_bus = SessionBus()
        self._watch_conn_name = None

         # Monitor IPv4 address as an indicator of the network connection
        self._ip4am = psutils.IP4AddressMonitor.get_instance()
        self._ip4am_sigid = self._ip4am.connect('address-changed',
                self._ip4_address_changed_cb)

    @property
    def status(self):
        """Return the Telepathy connection status."""
        return self._conn_status

    def get_connection(self):
        """Retrieve our telepathy.client.Connection object"""
        return self._conn

    def _get_account_info(self):
        """Retrieve connection manager parameters for this account
        """
        raise NotImplementedError

    def suggest_room_for_activity(self, activity_id):
        """Suggest a room to use to share the given activity.
        """
        return activity_id

    def identify_contacts(self, tp_chan, handles, identifiers=None):
        raise NotImplementedError

    def _reconnect_cb(self):
        """Attempt to reconnect to the server after the back-off time has
        elapsed.
        """
        _logger.debug("%r: reconnect timed out. Let's try to connect", self)
        if self._backoff_id > 0:
            gobject.source_remove(self._backoff_id)
            self._backoff_id = 0

        # this is a no-op unless _could_connect() returns True
        self.start()

        return False

    def _reset_reconnect_timer(self):
        if self._backoff_id != 0:
            gobject.source_remove(self._backoff_id)

        _logger.debug("%r: restart reconnect time out (%u seconds)",
                self, self._reconnect_timeout / 1000)
        self._backoff_id = gobject.timeout_add(self._reconnect_timeout,
                self._reconnect_cb)

        if self._reconnect_timeout < self._RECONNECT_MAX_TIMEOUT:
            self._reconnect_timeout *= 2

    def _init_connection(self):
        """Set up our connection

        if there is no existing connection
            (_find_existing_connection returns None)
        produce a new connection with our protocol for our
        account.

        if there is an existing connection, reuse it by
        registering for various of events on it.
        """
        _logger.debug('%r: init connection', self)
        conn = self._find_existing_connection()
        if not conn:
            _logger.debug('%r: no existing connection. Create a new one', self)
            conn = self._make_new_connection()
        else:
            _logger.debug('%r: found existing connection. Reuse it', self)

        m = conn[CONN_INTERFACE].connect_to_signal('StatusChanged',
           self._handle_connection_status_change)
        self._matches.append(m)
        m = conn[CONN_INTERFACE].connect_to_signal('NewChannel',
                                                   self._new_channel_cb)
        self._matches.append(m)
        self._watch_conn_name = self._session_bus.watch_name_owner(
            conn.service_name, self._watch_conn_name_cb)

        self._conn = conn
        status = self._conn[CONN_INTERFACE].GetStatus()

        self._owner.set_properties_before_connect(self)

        if status == CONNECTION_STATUS_DISCONNECTED:
            def connect_reply():
                _logger.debug('%r: Connect() succeeded', self)
            def connect_error(e):
                _logger.debug('%r: Connect() failed: %s', self, e)
                # we don't allow ourselves to retry more often than this
                self._reset_reconnect_timer()
                self._conn = None

            self._conn[CONN_INTERFACE].Connect(reply_handler=connect_reply,
                                               error_handler=connect_error)

        self._handle_connection_status_change(status,
                CONNECTION_STATUS_REASON_NONE_SPECIFIED)

    def _find_existing_connection(self):
        raise NotImplementedError

    def _make_new_connection(self):
        acct = self._account.copy()

        # Create a new connection
        mgr = self._registry.GetManager(self._TP_CONN_MANAGER)
        name, path = mgr[CONN_MGR_INTERFACE].RequestConnection(
            self._PROTOCOL, acct)
        conn = Connection(name, path)
        del acct
        return conn

    def _watch_conn_name_cb(self, dbus_name):
        """Check if we still have a connection on the DBus session bus.

        If the connection disappears, stop the plugin.
        """
        if not dbus_name and self._conn is not None:
            _logger.warning(
                'D-Bus name %s disappeared, this probably means %s crashed',
                self._conn.service_name, self._TP_CONN_MANAGER)
            self._handle_connection_status_change(
                CONNECTION_STATUS_DISCONNECTED, 
                CONNECTION_STATUS_REASON_NONE_SPECIFIED)

    def _handle_connection_status_change(self, status, reason):
        if status == self._conn_status:
            return

        if status == CONNECTION_STATUS_CONNECTING:
            self._conn_status = status
            _logger.debug("%r: connecting...", self)
        elif status == CONNECTION_STATUS_CONNECTED:
            self._conn_status = status
            _logger.debug("%r: connected", self)
            self._connected_cb()
        elif status == CONNECTION_STATUS_DISCONNECTED:
            self._conn = None
            self._stop()
            _logger.debug("%r: disconnected (reason %r)", self, reason)
            if reason == CONNECTION_STATUS_REASON_AUTHENTICATION_FAILED:
                # FIXME: handle connection failure; retry later?
                _logger.debug("%r: authentification failed. Give up ", self)
                pass
            else:
                # Try again later. We'll detect whether we have a network
                # connection after the retry period elapses. The fact that
                # this timer is running also serves as a marker to indicate
                # that we shouldn't try to go back online yet.
                self._reset_reconnect_timer()

        self.emit('status', self._conn_status, int(reason))

    def _could_connect(self):
        # Don't allow connection unless the reconnect timeout has elapsed,
        # or this is the first attempt
        return (self._backoff_id == 0)

    def _stop(self):
        """If we still have a connection, disconnect it"""

        matches = self._matches
        self._matches = []
        for match in matches:
            match.remove()
        if self._watch_conn_name is not None:
            self._watch_conn_name.cancel()
        self._watch_conn_name = None

        if self._conn:
            try:
                self._conn[CONN_INTERFACE].Disconnect()
            except DBusException, e:
                _logger.debug('%s Disconnect(): %s', self._conn.object_path, e)

        self._conn_status = CONNECTION_STATUS_DISCONNECTED
        self.emit('status', self._conn_status, 0)

        if self._online_contacts:
            # Copy contacts when passing them to self._contacts_offline to
            # ensure it's pass by _value_, otherwise (since it's a set) it's
            # passed by reference and odd things happen when it gets subtracted
            # from itself
            self._contacts_offline(self._online_contacts.copy())

        # Erase connection as the last thing done, because some of the 
        # above calls depend on self._conn being valid
        self._conn = None

    def cleanup(self):
        self._stop()

        if self._backoff_id > 0:
            gobject.source_remove(self._backoff_id)
            self._backoff_id = 0

        self._ip4am.disconnect(self._ip4am_sigid)
        self._ip4am_sigid = 0

    def _contacts_offline(self, handles):
        """Handle contacts going offline (send message, update set)"""
        self._online_contacts -= handles
        _logger.debug('%r: Contacts now offline: %r', self, handles)
        self.emit("contacts-offline", handles)

    def _contacts_online(self, handles):
        """Handle contacts coming online"""
        relevant = []

        for handle in handles:
            if handle == self.self_handle:
                # ignore network events for Owner property changes since those
                # are handled locally
                pass
            elif (handle in self._subscribe_members or
                  handle in self._subscribe_local_pending or
                  handle in self._subscribe_remote_pending):
                relevant.append(handle)
            # else it's probably a channel-specific handle - can't create a
            # Buddy object for those yet

        if not relevant:
            return

        jids = self._conn[CONN_INTERFACE].InspectHandles(
                HANDLE_TYPE_CONTACT, relevant)

        handle_to_objid = self.identify_contacts(None, relevant, jids)
        objids = []
        for handle in relevant:
            objids.append(handle_to_objid[handle])

        self._online_contacts |= frozenset(relevant)
        _logger.debug('%r: Contacts now online:', self)
        for handle, objid in izip(relevant, objids):
            _logger.debug('  %u .../%s', handle, objid)
        self.emit('contacts-online', objids, relevant, jids)

    def _subscribe_members_changed_cb(self, message, added, removed,
                                      local_pending, remote_pending,
                                      actor, reason):

        added = set(added)
        removed = set(removed)
        local_pending = set(local_pending)
        remote_pending = set(remote_pending)

        affected = added|removed
        affected |= local_pending
        affected |= remote_pending

        self._subscribe_members -= affected
        self._subscribe_members |= added
        self._subscribe_local_pending -= affected
        self._subscribe_local_pending |= local_pending
        self._subscribe_remote_pending -= affected
        self._subscribe_remote_pending |= remote_pending

    def _publish_members_changed_cb(self, message, added, removed,
            local_pending, remote_pending, actor, reason):
        pass

    def _presence_update_cb(self, presence):
        """Send update for online/offline status of presence"""

        now_online = set()
        now_offline = set(presence.iterkeys())

        for handle in presence:
            timestamp, statuses = presence[handle]
            for status, params in statuses.items():
                # FIXME: use correct logic involving the GetStatuses method
                if status in ["available", "away", "brb", "busy", "dnd", "xa"]:
                    now_online.add(handle)
                    now_offline.discard(handle)

        now_online -= self._online_contacts
        now_offline &= self._online_contacts

        if now_online:
            self._contacts_online(now_online)
        if now_offline:
            self._contacts_offline(now_offline)

    def _new_channel_cb(self, object_path, channel_type, handle_type, handle,
                        suppress_handler):
        """Handle creation of a new channel
        """
        if (handle_type == HANDLE_TYPE_ROOM and
            channel_type == CHANNEL_TYPE_TEXT):

            def ready(channel):
                # workaround for odd behaviour of nested scopes
                room_self = []

                def got_lpwi(info):
                    for invitee, actor, reason, message in info:
                        if ((invitee == room_self[0]
                             or invitee == self.self_handle)
                            and reason == CHANNEL_GROUP_CHANGE_REASON_INVITED):
                            self.emit('activity-invitation', channel, handle,
                                      actor, message)
                def got_lpwi_err(e):
                    _logger.warning('Unable to get channel members for %s:',
                                    object_path, exc_info=1)

                def got_self_handle(self_handle):
                    room_self.append(self_handle)
                    group.GetLocalPendingMembersWithInfo(
                            reply_handler=got_lpwi,
                            error_handler=got_lpwi_err)
                def got_self_handle_err(e):
                    _logger.warning('Unable to get self-handle for %s:',
                                    object_path, exc_info=1)

                group = channel[CHANNEL_INTERFACE_GROUP]
                group.GetSelfHandle(reply_handler=got_self_handle,
                                    error_handler=got_self_handle_err)

            # we throw away the channel as soon as ready() finishes
            Channel(self._conn.service_name, object_path,
                    ready_handler=ready)

        elif (handle_type == HANDLE_TYPE_CONTACT and
              channel_type in (CHANNEL_TYPE_TEXT,
                               CHANNEL_TYPE_STREAMED_MEDIA)):
            self.emit("private-invitation", object_path, channel_type)

        elif (handle_type == HANDLE_TYPE_LIST and
              channel_type == CHANNEL_TYPE_CONTACT_LIST):
            name = self._conn.InspectHandles(handle_type, [handle])[0]
            channel = Channel(self._conn.service_name, object_path)

            if name == 'publish':
                self._publish_channel_cb(channel)
            elif name == 'subscribe':
                self._subscribe_channel_cb(channel)

    def _publish_channel_cb(self, channel):
        # the group of contacts who may receive your presence
        self._publish_channel = channel
        m = channel[CHANNEL_INTERFACE_GROUP].connect_to_signal(
                'MembersChanged', self._publish_members_changed_cb)
        self._matches.append(m)

    def _subscribe_channel_cb(self, channel):
        # the group of contacts for whom you wish to receive presence
        self._subscribe_channel = channel
        m = channel[CHANNEL_INTERFACE_GROUP].connect_to_signal(
                'MembersChanged', self._subscribe_members_changed_cb)
        self._matches.append(m)
        subscribe_handles, subscribe_lp, subscribe_rp = \
                channel[CHANNEL_INTERFACE_GROUP].GetAllMembers()
        self._subscribe_members = set(subscribe_handles)
        self._subscribe_local_pending = set(subscribe_lp)
        self._subscribe_remote_pending = set(subscribe_rp)

        if CONN_INTERFACE_PRESENCE in self._conn:
            # request presence for everyone we're subscribed to
            self._conn[CONN_INTERFACE_PRESENCE].RequestPresence(
                    subscribe_handles)

    def _connected_cb(self):
        """Callback on successful connection to a server
        """
        # FIXME: cope with CMs that lack some of the interfaces
        # FIXME: cope with CMs with no 'publish' or 'subscribe'

        # FIXME: retry if getting the channel times out

        interfaces = self._conn.get_valid_interfaces()

        # FIXME: this is a hack, but less harmful than the previous one -
        # the next version of telepathy-python will contain a better fix
        for iface in self._conn[CONN_INTERFACE].GetInterfaces():
            interfaces.add(iface)

        # FIXME: do this async?
        self.self_handle = self._conn[CONN_INTERFACE].GetSelfHandle()
        self.self_identifier = self._conn[CONN_INTERFACE].InspectHandles(
                HANDLE_TYPE_CONTACT, [self.self_handle])[0]

        if CONN_INTERFACE_PRESENCE in self._conn:
            # Ask to be notified about presence changes
            m = self._conn[CONN_INTERFACE_PRESENCE].connect_to_signal(
                    'PresenceUpdate', self._presence_update_cb)
            self._matches.append(m)
        else:
            _logger.warning('%s does not support Connection.Interface.'
                            'Presence', self._conn.object_path)

    def start(self):
        """Start up the Telepathy networking connections

        if we are already connected, query for the initial contact
        information.

        if we are already connecting, do nothing

        otherwise initiate a connection and transfer control to
            _connect_reply_cb or _connect_error_cb
        """

        if self._ip4am_sigid == 0:
            self._ip4am_sigid = self._ip4am.connect('address-changed',
                    self._ip4_address_changed_cb)

        if self._conn is not None:
            return

        _logger.debug("%r: Starting up...", self)

        # Only init connection if we have a valid IP address
        if self._could_connect():
            self._init_connection()
        else:
            _logger.debug('%r: Postponing connection', self)

    def _ip4_address_changed_cb(self, ip4am, address, iface):
        _logger.debug("::: IP4 address now %s", address)

        self._reconnect_timeout = self._RECONNECT_INITIAL_TIMEOUT
