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

import gobject
import dbus
import dbus.service
from dbus.gobject_service import ExportedGObject
from sugar import util
import logging

from telepathy.client import Channel
from telepathy.constants import (CHANNEL_GROUP_FLAG_CHANNEL_SPECIFIC_HANDLES,
                                 PROPERTY_FLAG_WRITE, HANDLE_TYPE_ROOM)
from telepathy.interfaces import (CHANNEL_INTERFACE, CHANNEL_INTERFACE_GROUP,
                                  CHANNEL_TYPE_TEXT, CONN_INTERFACE,
                                  PROPERTIES_INTERFACE)

from psutils import NotFoundError, NotJoinedError, WrongConnectionError


CONN_INTERFACE_ACTIVITY_PROPERTIES = 'org.laptop.Telepathy.ActivityProperties'

_ACTIVITY_PATH = "/org/laptop/Sugar/Presence/Activities/"
_ACTIVITY_INTERFACE = "org.laptop.Sugar.Presence.Activity"

_PROP_ID = "id"
_PROP_NAME = "name"
_PROP_COLOR = "color"
_PROP_TYPE = "type"
_PROP_TAGS = "tags"
_PROP_VALID = "valid"
_PROP_LOCAL = "local"
_PROP_JOINED = "joined"
_PROP_PRIVATE = "private"
_PROP_CUSTOM_PROPS = "custom-props"

_logger = logging.getLogger('s-p-s.activity')

class Activity(ExportedGObject):
    """Represents a shared activity seen on the network, or a local activity
    that has been, or will be, shared onto the network.

    The activity might be public, restricted to a group, or invite-only.
    """

    __gtype_name__ = "Activity"

    __gsignals__ = {
        'validity-changed':
            # The activity's validity has changed.
            # An activity is valid if its name, color, type and ID have been
            # set.
            # Arguments:
            #   validity: bool
            (gobject.SIGNAL_RUN_FIRST, None, [bool]),
        'disappeared':
            # Nobody is in this activity any more.
            # No arguments.
            (gobject.SIGNAL_RUN_FIRST, None, []),
    }

    __gproperties__ = {
        _PROP_ID           : (str, None, None, None,
                              gobject.PARAM_READWRITE |
                              gobject.PARAM_CONSTRUCT_ONLY),
        _PROP_NAME         : (object, None, None, gobject.PARAM_READWRITE),
        _PROP_TAGS         : (object, None, None, gobject.PARAM_READWRITE),
        _PROP_COLOR        : (str, None, None, None, gobject.PARAM_READWRITE),
        _PROP_TYPE         : (str, None, None, None, gobject.PARAM_READWRITE),
        _PROP_PRIVATE      : (bool, None, None, True, gobject.PARAM_READWRITE),
        _PROP_VALID        : (bool, None, None, False, gobject.PARAM_READABLE),
        _PROP_LOCAL        : (bool, None, None, False,
                              gobject.PARAM_READWRITE |
                              gobject.PARAM_CONSTRUCT_ONLY),
        _PROP_JOINED       : (bool, None, None, False, gobject.PARAM_READABLE),
        _PROP_CUSTOM_PROPS : (object, None, None,
                              gobject.PARAM_READWRITE |
                              gobject.PARAM_CONSTRUCT_ONLY)
    }

    _RESERVED_PROPNAMES = __gproperties__.keys()

    def __init__(self, bus, object_id, ps, tp, room, **kwargs):
        """Initializes the activity and sets its properties to default values.

        :Parameters:
            `bus` : dbus.bus.BusConnection
                A connection to the D-Bus session bus
            `object_id` : int
                PS ID for this activity, used to construct the object-path
            `ps` : presenceservice.PresenceService
                The presence service
            `tp` : server plugin
                The server plugin object (stands for "telepathy plugin")
            `room` : int or long
                The handle (of type HANDLE_TYPE_ROOM) of the activity on
                the server plugin
        :Keywords:
            `id` : str
                The globally unique activity ID (required)
            `name` : unicode
                Human-readable title for the activity
            `tags` : unicode
                Tags for this activity
            `color` : str
                Activity color in #RRGGBB,#RRGGBB (stroke,fill) format
            `type` : str
                D-Bus service name representing the activity type
            `local : bool
                If True, this activity was initiated locally and is not
                (yet) advertised on the network
                (FIXME: is this description right?)
            `private` : bool
                If True, this activity is not advertised to everyone
            `custom-props` : dict
                Activity-specific properties
        """

        if not object_id or not isinstance(object_id, int):
            raise ValueError("object id must be a valid number")
        if not tp:
            raise ValueError("telepathy CM must be valid")

        self._ps = ps
        self._object_id = object_id
        self._object_path = dbus.ObjectPath(_ACTIVITY_PATH +
                                            str(self._object_id))

        self._buddies = set()
        self._member_handles = set()
        self._joined = False

        self._join_cb = None
        self._join_err_cb = None
        self._join_is_sharing = False
        self._private = True
        self._leave_cb = None
        self._leave_err_cb = None

        # the telepathy client
        self._tp = tp
        self._room = room
        self._self_handle = None
        self._text_channel = None
        self._text_channel_group_flags = 0
        #: list of SignalMatch associated with the text channel, or None
        self._text_channel_matches = None

        self._valid = False
        self._id = None
        self._actname = None
        self._color = None
        self._private = True
        self._tags = u''
        self._local = False
        self._type = None
        self._custom_props = {}

        # ensure no reserved property names are in custom properties
        cprops = kwargs.get(_PROP_CUSTOM_PROPS)
        if cprops is not None:
            (rprops, cprops) = self._split_properties(cprops)
            if len(rprops.keys()) > 0:
                raise ValueError("Cannot use reserved property names '%s'"
                                 % ", ".join(rprops.keys()))

        if not kwargs.get(_PROP_ID):
            raise ValueError("activity id is required")
        if not util.validate_activity_id(kwargs[_PROP_ID]):
            raise ValueError("Invalid activity id '%s'" % kwargs[_PROP_ID])

        ExportedGObject.__init__(self, bus, self._object_path,
                                 gobject_properties=kwargs)
        if self._local and not self._valid:
            raise RuntimeError("local activities require color, type, and "
                               "name")

        # If not yet valid, query activity properties
        if not self._valid:
            assert self._room, self._room
            conn = self._tp.get_connection()

            if CONN_INTERFACE_ACTIVITY_PROPERTIES not in conn:
                # we should already have warned about this somewhere -
                # certainly, don't emit a warning per activity!
                return

            def got_properties_err(e):
                _logger.warning('Failed to get initial activity properties '
                                'for %s: %s', self._id, e)

            conn[CONN_INTERFACE_ACTIVITY_PROPERTIES].GetProperties(self._room,
                    reply_handler=self.set_properties,
                    error_handler=got_properties_err)

    @property
    def room_details(self):
        """Return the Telepathy plugin on which this Activity can be joined
        and the handle of the room representing it.
        """
        return (self._tp, self._room)

    def do_get_property(self, pspec):
        """Gets the value of a property associated with this activity.

        pspec -- Property specifier

        returns The value of the given property.
        """

        if pspec.name == _PROP_ID:
            return self._id
        elif pspec.name == _PROP_NAME:
            return self._actname
        elif pspec.name == _PROP_TAGS:
            return self._tags
        elif pspec.name == _PROP_COLOR:
            return self._color
        elif pspec.name == _PROP_TYPE:
            return self._type
        elif pspec.name == _PROP_PRIVATE:
            return self._private
        elif pspec.name == _PROP_VALID:
            return self._valid
        elif pspec.name == _PROP_JOINED:
            return self._joined
        elif pspec.name == _PROP_LOCAL:
            return self._local

    def do_set_property(self, pspec, value):
        """Sets the value of a property associated with this activity.

        pspec -- Property specifier
        value -- Desired value

        Note that the "type" property can be set only once; attempting to set
        it to something different later will raise a RuntimeError.

        """
        if pspec.name == _PROP_ID:
            if self._id:
                raise RuntimeError("activity ID is already set")
            self._id = value
        elif pspec.name == _PROP_NAME:
            self._actname = unicode(value)
        elif pspec.name == _PROP_COLOR:
            self._color = value
        elif pspec.name == _PROP_PRIVATE:
            self._private = value
        elif pspec.name == _PROP_TAGS:
            self._tags = unicode(value)
        elif pspec.name == _PROP_TYPE:
            if self._type:
                raise RuntimeError("activity type is already set")
            self._type = value
        elif pspec.name == _PROP_JOINED:
            self._joined = value
        elif pspec.name == _PROP_LOCAL:
            self._local = value
        elif pspec.name == _PROP_CUSTOM_PROPS:
            if not value:
                value = {}
            (rprops, cprops) = self._split_properties(value)
            self._custom_props = {}
            for (key, dvalue) in cprops.items():
                self._custom_props[str(key)] = str(dvalue)

        self._update_validity()

    def _update_validity(self):
        """Sends a "validity-changed" signal if this activity's validity has
        changed.

        Determines whether this activity's status has changed from valid to
        invalid, or invalid to valid, and emits a "validity-changed" signal
        if either is true.  "Valid" means that the object's type, ID, name,
        colour and type properties have all been set to something valid
        (i.e., not "None").

        """
        try:
            old_valid = self._valid
            if (self._color is not None and self._actname is not None
                and self._id is not None and self._type is not None):
                self._valid = True
            else:
                self._valid = False

            if old_valid != self._valid:
                self.emit("validity-changed", self._valid)
                if self._valid:
                    # Pretend everyone joined
                    for buddy in self._buddies:
                        self.BuddyJoined(buddy.object_path())
                else:
                    # Pretend everyone left
                    for buddy in self._buddies:
                        self.BuddyLeft(buddy.object_path())

        except AttributeError:
            self._valid = False

    # dbus signals
    @dbus.service.signal(_ACTIVITY_INTERFACE,
                        signature="o")
    def BuddyJoined(self, buddy_path):
        """Generates DBUS signal when a buddy joins this activity.

        buddy_path -- DBUS path to buddy object
        """
        _logger.debug('BuddyJoined: %s', buddy_path)

    @dbus.service.signal(_ACTIVITY_INTERFACE,
                        signature="o")
    def BuddyLeft(self, buddy_path):
        """Generates DBUS signal when a buddy leaves this activity.

        buddy_path -- DBUS path to buddy object
        """
        _logger.debug('BuddyLeft: %s', buddy_path)

    @dbus.service.signal(_ACTIVITY_INTERFACE,
                        signature="a{sv}")
    def PropertiesChanged(self, properties):
        """Emits D-Bus signal when properties of this activity change.

        The properties dict is the same as for GetProperties, but omits
        properties that have not actually changed.
        """
        _logger.debug('Emitting PropertiesChanged: %r', properties)

    @dbus.service.signal(_ACTIVITY_INTERFACE,
                        signature="o")
    def NewChannel(self, channel_path):
        """Generates DBUS signal when a new channel is created for this
        activity.

        channel_path -- DBUS path to new channel

        XXX - what is this supposed to do?  Who is supposed to call it?
        What is the channel path?  Right now this is never called.

        """
        pass

    # dbus methods
    @dbus.service.method(_ACTIVITY_INTERFACE,
                        in_signature="", out_signature="a{sv}")
    def GetProperties(self):
        """D-Bus method to get this activity's properties.

        The keys of the dict are defined by Presence Service. Currently
        the possible keys are:

        `private` : bool
            If False, the activity is advertised to everyone
        `name` : unicode
            The name of the activity - '' if not known yet
        `tags` : unicode
            The activity's tags (initially '')
        `color` : string of the form #112233,#456789
            The activity's icon color - '' if not known yet
        `type` : string in the same format as a D-Bus well-known name
            The activity type (cannot change) - '' if not known yet
        `id` : string
            The activity ID (cannot change) - '' if not known yet
        """
        ret = {_PROP_PRIVATE: self._private,
               _PROP_NAME: self._actname or u'',
               _PROP_TAGS: self._tags,
               _PROP_COLOR: self._color or '',
               _PROP_TYPE: self._type or '',
               _PROP_ID: self._id or '',
               }
        _logger.debug('%r', ret)
        return ret

    @dbus.service.method(_ACTIVITY_INTERFACE,
                        in_signature="", out_signature="s")
    def GetId(self):
        """DBUS method to get this activity's (randomly generated) unique ID

        :Returns: Activity ID as a string
        """
        return self._id or ''

    @dbus.service.method(_ACTIVITY_INTERFACE,
                        in_signature="", out_signature="s")
    def GetColor(self):
        """DBUS method to get this activity's colour

        :Returns: Activity colour as a string in the format #RRGGBB,#RRGGBB
        """
        return self._color or ''

    @dbus.service.method(_ACTIVITY_INTERFACE,
                        in_signature="", out_signature="s")
    def GetType(self):
        """DBUS method to get this activity's type

        :Returns: Activity type as a string, in the same form as a D-Bus
            well-known name
        """
        return self._type or ''

    @dbus.service.method(_ACTIVITY_INTERFACE,
                         in_signature='os', out_signature='',
                         async_callbacks=('async_cb', 'async_err_cb'))
    def Invite(self, buddy_path, message, async_cb, async_err_cb):
        """Invite a buddy to join this activity if they are not already in it.

        :Parameters:
            `buddy` : dbus.ObjectPath
                The buddy to be invited
            `message` : dbus.String
                A message to send to the buddy
        :Raises NotJoinedError: if we're not in the activity ourselves
        :Raises NotFoundError: if there is no such buddy
        :Raises WrongConnectionError: if the buddy is not visible on that
            Telepathy connection
        :Raises telepathy.errors.PermissionDenied: if we can't invite the buddy
        """
        if not self._joined:
            _logger.debug('Not inviting %s into %s: I am not a member',
                          buddy_path, self._id)
            async_err_cb(NotJoinedError("Can't invite buddies into an "
                                        "activity you haven't yourself "
                                        "joined"))
            return

        assert self._tp is not None
        assert self._text_channel is not None

        buddy = self._ps.get_buddy_by_path(buddy_path)
        if buddy is None:
            _logger.debug('Not inviting nonexistent buddy %s', buddy_path)
            async_err_cb(NotFoundError('Buddy not found: %s' % buddy_path))
            return

        if buddy in self._buddies:
            # nothing to do
            _logger.debug('Not inviting %s into %s: already a member',
                          buddy_path, self._id)
            async_cb()
            return

        # actually invite them
        buddy_ident = buddy.get_identifier_by_plugin(self._tp)
        if buddy_ident is None:
            conn_path = self._tp.get_connection().object_path
            _logger.debug('Activity %s is on connection %s but buddy %s is '
                          'not', self._id, conn_path, buddy_path)
            async_err_cb(WrongConnectionError('Buddy %s cannot be '
                'invited to activity %s: the buddy is not on the '
                'Telepathy connection %s'
                % (buddy_path, self._id, conn_path)))
        else:
            _logger.debug('Inviting buddy %s to activity %s via handle #%d '
                          '<%s>', buddy_path, self._id, buddy_ident[0],
                          buddy_ident[1])
            self._text_channel.AddMembers([buddy_ident[0]], message,
                    dbus_interface=CHANNEL_INTERFACE_GROUP,
                    reply_handler=async_cb,
                    error_handler=async_err_cb)

    @dbus.service.method(_ACTIVITY_INTERFACE,
                         in_signature="", out_signature="",
                         async_callbacks=('async_cb', 'async_err_cb'))
    def Join(self, async_cb, async_err_cb):
        """DBUS method for the local user to attempt to join the activity

        async_cb -- Callback method to be called if join attempt is successful
        async_err_cb -- Callback method to be called if join attempt is
                        unsuccessful

        """
        self.join(async_cb, async_err_cb, False)

    @dbus.service.method(_ACTIVITY_INTERFACE,
                         in_signature="", out_signature="",
                         async_callbacks=('async_cb', 'async_err_cb'))
    def Leave(self, async_cb, async_err_cb):
        """DBUS method to for the local user to leave the shared activity

        async_cb -- Callback method to be called if join attempt is successful
        async_err_cb -- Callback method to be called if join attempt is
                        unsuccessful

        """
        self.leave(async_cb, async_err_cb)

    @dbus.service.method(_ACTIVITY_INTERFACE,
                        in_signature="", out_signature="ao")
    def GetJoinedBuddies(self):
        """DBUS method to return a list of valid buddies who are joined in
        this activity

        :Returns:
            A list of buddy object paths corresponding to those buddies
            in this activity who are 'valid' (i.e. for whom we have complete
            information)
        """
        ret = []
        for buddy in self._buddies:
            if buddy.props.valid:
                ret.append(buddy.object_path())
        return ret

    @dbus.service.method(_ACTIVITY_INTERFACE,
                        in_signature="", out_signature="soao")
    def GetChannels(self):
        """DBUS method to get the list of channels associated with this
        activity

        :Returns:
            a tuple containing:
                - the D-Bus well-known service name of the connection
                  (FIXME: this is redundant; in Telepathy it can be derived
                  from that of the connection)
                - the D-Bus object path of the connection
                - a list of D-Bus object paths representing the channels
                  associated with this activity
        """
        return self.get_channels()

    @dbus.service.method(_ACTIVITY_INTERFACE,
                         in_signature='a{sv}', out_signature='')
    def SetProperties(self, new_props):
        """D-Bus method to update the activity's properties.

        The parameter has the same keys as for GetProperties(); missing
        keys are treated as unchanged.
        """
        if not self._joined:
            raise NotJoinedError('Not in activity %s' % self._id)

        changed = set()

        val = new_props.pop(_PROP_TYPE, None)
        if val is not None:
            if self._type != val:
                raise ValueError('"type" property may not change')

        val = new_props.pop(_PROP_ID, None)
        if val is not None:
            if self._id != val:
                raise ValueError('"id" property may not change')

        val = new_props.pop(_PROP_PRIVATE, None)
        if val is not None:
            if not isinstance(val, (bool, dbus.Boolean)):
                raise ValueError('"private" property must be boolean')
            if self._private != val:
                self._private = val
                changed.add(_PROP_PRIVATE)

        val = new_props.pop(_PROP_NAME, None)
        if val is not None:
            if not isinstance(val, unicode):
                raise ValueError('"name" property must be unicode string')
            if self._actname != val:
                self._actname = val
                changed.add(_PROP_NAME)

        val = new_props.pop(_PROP_TAGS, None)
        if val is not None:
            if not isinstance(val, unicode):
                raise ValueError('"tags" property must be unicode string')
            if self._tags != val:
                self._tags = val
                changed.add(_PROP_TAGS)

        val = new_props.pop(_PROP_COLOR, None)
        if val is not None:
            if not isinstance(val, unicode):
                raise ValueError('"color" property must be string')
            val = val.decode('ascii')
            if self._color != val:
                self._color = val
                changed.add(_PROP_COLOR)

        if changed:
            # FIXME: pass SetProperties errors back to caller too
            self.send_properties(changed)

        if new_props:
            raise ValueError('Unknown properties: %s' % new_props.keys())

    @dbus.service.method(_ACTIVITY_INTERFACE,
                        in_signature="", out_signature="s")
    def GetName(self):
        """DBUS method to get this activity's name

        returns Activity name
        """
        return self._actname or u''

    # methods
    def object_path(self):
        """Retrieves our dbus.ObjectPath object

        returns DBUS ObjectPath object
        """
        return self._object_path

    def get_joined_buddies(self):
        """Local method to return a list of valid buddies who are joined in
        this activity

        This method is called by the PresenceService on the local machine.

        returns A list of buddy objects
        """
        ret = []
        for buddy in self._buddies:
            if buddy.props.valid:
                ret.append(buddy)
        return ret

    def buddy_apparently_joined(self, buddy):
        """Adds a buddy to this activity and sends a BuddyJoined signal,
        unless we can already see who's in the activity by being in it
        ourselves.

        buddy -- Buddy object representing the buddy being added

        Adds a buddy to this activity if the buddy is not already in the
        buddy list.

        If this activity is "valid", a BuddyJoined signal is also sent.
        This method is called by the PresenceService on the local machine.

        """
        if self._joined:
            _logger.debug("Ignoring alleged join to activity %s that I'm in: "
                          "I can already see who's there", self._id)
        else:
            _logger.debug("%s says they joined activity %s that I'm not in",
                          buddy.props.objid, self._id)
            self._add_buddies((buddy,))

    def _add_buddies(self, buddies):
        buddies = set(buddies)
        _logger.debug("Adding buddies: %r", buddies)

        # disregard any who are already there
        buddies -= self._buddies

        self._buddies |= buddies

        for buddy in buddies:
            buddy.add_activity(self)
            if self._valid:
                self.BuddyJoined(buddy.object_path())
            else:
                _logger.debug('Suppressing BuddyJoined: activity not "valid"')

    def _remove_buddies(self, buddies):
        buddies = set(buddies)
        _logger.debug("Removing buddies: %r", buddies)

        # disregard any who are not already there
        buddies &= self._buddies

        self._buddies -= buddies

        for buddy in buddies:
            buddy.remove_activity(self)
            if self._valid:
                self.BuddyLeft(buddy.object_path())
            else:
                _logger.debug('Suppressing BuddyLeft: activity not "valid"')

        if not self._buddies:
            self.emit('disappeared')

    def buddy_apparently_left(self, buddy):
        """Removes a buddy from this activity and sends a BuddyLeft signal,
        unless we can already see who's in the activity by being in it
        ourselves.

        buddy -- Buddy object representing the buddy being removed

        Removes a buddy from this activity if the buddy is in the buddy list.
        If this activity is "valid", a BuddyLeft signal is also sent.
        This method is called by the PresenceService on the local machine.
        """
        if not self._joined:
            self._remove_buddies((buddy,))
        else:
            # XXX Buddy-left starts working partially at least, if we do this anyway:
            self._remove_buddies((buddy,))

    def _text_channel_group_flags_changed_cb(self, added, removed):
        self._text_channel_group_flags |= added
        self._text_channel_group_flags &= ~removed

    def _clean_up_matches(self):
        matches = self._text_channel_matches
        self._text_channel_matches = []
        if matches is not None:
            for match in matches:
                match.remove()

    def _joined_cb(self):
        """XXX - not documented yet
        """
        self._ps.owner.add_owner_activity(self._tp, self._id, self._room)

        verb = self._join_is_sharing and 'Share' or 'Join'

        try:
            if self._join_is_sharing:
                self.send_properties()
                self._ps.owner.add_activity(self)
            self._join_cb()
            _logger.debug("%s of activity %s succeeded" % (verb, self._id))
        except Exception, e:
            self._join_failed_cb(e)

        self._join_cb = None
        self._join_err_cb = None

    def _join_failed_cb(self, e):
        verb = self._join_is_sharing and 'Share' or 'Join'
        _logger.debug("%s of activity %s failed: %s" % (verb, self._id, e))
        self._join_err_cb(e)

        self._join_cb = None
        self._join_err_cb = None

    def _join_activity_channel_props_listed_cb(self, prop_specs):
        # FIXME: invite-only ought to be set on private activities; but
        # since only the owner can change invite-only, that would break
        # activity scope changes.
        props = {
            'anonymous': False,   # otherwise buddy resolution breaks
            'invite-only': False, # anyone who knows about the channel can join
            'persistent': False,  # vanish when there are no members
            'private': True,      # don't appear in server room lists
        }
        props_to_set = []
        for ident, name, sig, flags in prop_specs:
            value = props.pop(name, None)
            if value is not None:
                if flags & PROPERTY_FLAG_WRITE:
                    props_to_set.append((ident, value))
                # FIXME: else error, but only if we're creating the room?
        # FIXME: if props is nonempty, then we want to set props that aren't
        # supported here - raise an error?

        if props_to_set:
            self._text_channel[PROPERTIES_INTERFACE].SetProperties(
                props_to_set, reply_handler=self._joined_cb,
                error_handler=self._join_failed_cb)
        else:
            self._joined_cb()

    def _join_activity_create_channel_cb(self, chan_path):
        text_channel = Channel(self._tp.get_connection().service_name,
                chan_path)
        self_ident = self._ps.owner.get_identifier_by_plugin(self._tp)
        assert self_ident is not None

        self._text_channel = text_channel
        self.NewChannel(text_channel.object_path)
        self._clean_up_matches()

        m = self._text_channel[CHANNEL_INTERFACE].connect_to_signal('Closed',
                self._text_channel_closed_cb)
        self._text_channel_matches.append(m)

        # FIXME: do all this asynchronously
        # FIXME: cope with non-Group channels?

        group = text_channel[CHANNEL_INTERFACE_GROUP]
        self._self_handle = group.GetSelfHandle()

        self._text_channel_group_flags = 0
        m = group.connect_to_signal('GroupFlagsChanged',
                                    self._text_channel_group_flags_changed_cb)
        self._text_channel_matches.append(m)
        self._text_channel_group_flags = group.GetGroupFlags()

        # by the time we hook this, we need to know the group flags
        m = group.connect_to_signal('MembersChanged',
                                    self._text_channel_members_changed_cb)
        self._text_channel_matches.append(m)
        # bootstrap by getting the current state. This is where we find
        # out whether anyone was lying to us in their PEP info
        members, local_pending, remote_pending = group.GetAllMembers()
        members = set(members)
        added = members - self._member_handles
        removed = self._member_handles - members
        if added or removed:
            self._text_channel_members_changed_cb('', added, removed,
                                                  (), (), 0, 0)

        if self_ident[0] in local_pending:
            _logger.debug('I am local pending - entering room')
            group.AddMembers([self_ident[0]], '',
                reply_handler=lambda: None,
                error_handler=self._join_failed_cb)
        elif self._self_handle in local_pending:
            _logger.debug('I am local pending with channel-specific handle - '
                          'entering room')
            group.AddMembers([self._self_handle], '',
                reply_handler=lambda: None,
                error_handler=self._join_failed_cb)
        elif self._self_handle in members:
            _logger.debug('I am already in the room')
            assert self._joined     # set by _text_channel_members_changed_cb

    def _join_activity_got_handles_cb(self, handles):
        assert len(handles) == 1

        self._room = handles[0]

        conn = self._tp.get_connection()
        conn[CONN_INTERFACE].RequestChannel(CHANNEL_TYPE_TEXT,
            HANDLE_TYPE_ROOM, self._room, True,
            reply_handler=self._join_activity_create_channel_cb,
            error_handler=self._join_failed_cb)

    def join(self, async_cb, async_err_cb, sharing, private=True):
        """Local method for the local user to attempt to join the activity.

        async_cb -- Callback method to be called with no parameters
            if join attempt is successful
        async_err_cb -- Callback method to be called with an Exception
            parameter if join attempt is unsuccessful
        sharing -- bool: True if sharing, False if joining
        private -- bool: True if by invitation, False if Advertising

        The two callbacks are passed to the server_plugin ("tp") object,
        which in turn passes them back as parameters in a callback to the
        _joined_cb method; this callback is set up within this method.
        """
        _logger.debug("Starting share/join of activity %s", self._id)

        if self._joined:
            async_err_cb(RuntimeError("Already joined activity %s"
                                      % self._id))
            return

        if self._join_cb is not None:
            # FIXME: or should we trigger all the attempts?
            async_err_cb(RuntimeError('Already trying to join activity %s'
                                      % self._id))
            return

        self._join_cb = async_cb
        self._join_err_cb = async_err_cb
        self._join_is_sharing = sharing
        self._private = private

        if self._room:
            # we already know what the room is => we must be joining someone
            # else's activity?
            self._join_activity_got_handles_cb((self._room,))
        elif self._local:
            # we need to create a room
            conn = self._tp.get_connection()

            conn[CONN_INTERFACE].RequestHandles(HANDLE_TYPE_ROOM,
                [self._tp.suggest_room_for_activity(self._id)],
                reply_handler=self._join_activity_got_handles_cb,
                error_handler=self._join_failed_cb)
        else:
            async_err_cb(RuntimeError("Don't know what room to join for "
                                      "non-local activity %s" % self._id))

        _logger.debug("triggered share/join attempt on activity %s", self._id)

    def get_channels(self):
        """Local method to get the list of channels associated with this
        activity

        returns XXX - expected a list of channels, instead returning a tuple?
        """
        conn = self._tp.get_connection()
        # FIXME add tubes and others channels
        channels = []
        if self._text_channel is not None:
            channels.append(self._text_channel.object_path)
        return (str(conn.service_name), conn.object_path, channels)

    def leave(self, async_cb, async_err_cb):
        """Local method for the local user to leave the shared activity.

        async_cb -- Callback method to be called with no parameters
            if join attempt is successful
        async_err_cb -- Callback method to be called with an Exception
            parameter if join attempt is unsuccessful

        The two callbacks are passed to the server_plugin ("tp") object,
        which in turn passes them back as parameters in a callback to the
        _left_cb method; this callback is set up within this method.
        """
        _logger.debug("Leaving shared activity %s", self._id)
        if not self._joined:
            _logger.debug("Error: Had not joined activity %s" % self._id)
            async_err_cb(RuntimeError("Had not joined activity %s"
                                      % self._id))
            return
        if self._leave_cb is not None:
            async_err_cb(RuntimeError('Already trying to leave activity %s'
                                      % self._id))
            return
        self._leave_cb = async_cb
        self._leave_err_cb = async_err_cb
        self._ps.owner.remove_owner_activity(self._tp, self._id)
        self._text_channel[CHANNEL_INTERFACE].Close()

    def _text_channel_members_changed_cb(self, message, added, removed,
                                         local_pending, remote_pending,
                                         actor, reason):
        _logger.debug('Text channel %u members changed: + %r, - %r, LP %r, '
                      'RP %r, message %r, actor %r, reason %r', self._room,
                      added, removed, local_pending, remote_pending,
                      message, actor, reason)
        # Note: D-Bus calls this with list arguments, but after GetMembers()
        # we call it with set and tuple arguments; we cope with any iterable.

        if (self._text_channel_group_flags &
            CHANNEL_GROUP_FLAG_CHANNEL_SPECIFIC_HANDLES):
            map_chan = self._text_channel
        else:
            # we have global handles here
            map_chan = None

        # disregard any who are already there
        added = set(added)
        added -= self._member_handles
        self._member_handles |= added

        # for added people, we need a Buddy object
        added_buddies = self._ps.map_handles_to_buddies(self._tp,
                                                        map_chan,
                                                        added)
        self._add_buddies(added_buddies.itervalues())

        # we treat all pending members as if they weren't there
        removed = set(removed)
        removed |= set(local_pending)
        removed |= set(remote_pending)
        # disregard any who aren't already there
        removed &= self._member_handles
        self._member_handles -= removed

        # for removed people, don't bother creating a Buddy just so we can
        # say it left. If we don't already have a Buddy object for someone,
        # then obviously they're not in self._buddies!
        removed_buddies = self._ps.map_handles_to_buddies(self._tp,
                                                          map_chan,
                                                          removed,
                                                          create=False)
        self._remove_buddies(removed_buddies.itervalues())

        # if we were among those removed, we'll have to start believing
        # the spoofable PEP-based activity tracking again.
        if self._self_handle not in self._member_handles and self._joined:
            self._text_channel_closed_cb()

        if self._self_handle in self._member_handles and not self._joined:
            self._joined = True
            if PROPERTIES_INTERFACE not in self._text_channel:
                self._join_activity_channel_props_listed_cb(())
            else:
                self._text_channel[PROPERTIES_INTERFACE].ListProperties(
                    reply_handler=self._join_activity_channel_props_listed_cb,
                    error_handler=self._join_failed_cb)

    def _text_channel_closed_cb(self):
        """Callback method called when the text channel is closed.

        This callback is set up in the _handle_share_join method.
        """
        self._joined = False
        self._self_handle = None
        self._text_channel = None
        _logger.debug('Text channel closed')
        try:
            self._remove_buddies([self._ps.owner])
        except Exception, e:
            _logger.debug(
                "Failed to remove you from %s: %s" % (self._id, e))
        if self._leave_cb and self._leave_err_cb:
            try:
                self._leave_cb()
                _logger.debug("Leaving of activity %s succeeded" % self._id)
            except Exception, e:
                _logger.debug("Leaving of activity %s failed: %s" % (self._id, e))
                self._leave_err_cb(e)
        self._clean_up_matches()
        self._leave_cb = None
        self._leave_err_cb = None

    def send_properties(self, changed=()):
        """Tells the Telepathy server what the properties of this activity are.

        """
        props = {}
        props['name'] = self._actname or u''
        props['color'] = self._color or ''
        props['type'] = self._type or ''
        props['private'] = self._private
        props['tags'] = self._tags

        conn = self._tp.get_connection()

        if CONN_INTERFACE_ACTIVITY_PROPERTIES not in conn:
            # we should already have warned about this somewhere
            return

        def properties_set(e=None):
            if e is None:
                _logger.debug('Successfully set activity properties for %s',
                              self._id)
                # signal it back to local processes too
                # FIXME: if we stopped ignoring Telepathy
                # ActivityPropertiesChanged signals from ourselves, we could
                # just use that...
                self.set_properties(props, changed)
            else:
                _logger.debug('Failed to set activity properties for %s: %s',
                              self._id, e)

        conn[CONN_INTERFACE_ACTIVITY_PROPERTIES].SetProperties(self._room,
                props, reply_handler=properties_set,
                error_handler=properties_set)

    def set_properties(self, properties, changed=()):
        """Sets properties for this activity from a Telepathy
        ActivityPropertiesChanged signal or the return from the Telepathy
        GetProperties method.

        properties - Dictionary object containing properties keyed by
                     property names
        changed - iterable over properties that have definitely changed

        Note that if any of the name, colour and/or type property values is
        changed from what it originally was, the update_validity method will
        be called, resulting in a "validity-changed" signal being generated.
        Called by the PresenceService on the local machine.
        """
        changed_properties = {}

        validity_maybe_changed  = False
        # split reserved properties from activity-custom properties
        (rprops, cprops) = self._split_properties(properties)

        val = rprops.get(_PROP_NAME, self._actname)
        if isinstance(val, unicode) and (_PROP_NAME in changed or
                                         val != self._actname):
            self._actname = val
            changed_properties[_PROP_NAME] = val
            validity_maybe_changed = True

        val = bool(rprops.get(_PROP_PRIVATE, self._private))
        if _PROP_PRIVATE in changed or val != self._private:
            changed_properties[_PROP_PRIVATE] = val
            self._private = val

        val = rprops.get(_PROP_TAGS, self._tags)
        if isinstance(val, unicode) and val != self._tags:
            changed_properties[_PROP_TAGS] = val
            self._tags = val

        val = rprops.get(_PROP_COLOR, self._color)
        if isinstance(val, unicode):
            try:
                val = val.encode('ascii')
            except UnicodeError:
                _logger.debug('Invalid color %s', val)
            else:
                if _PROP_COLOR in changed or val != self._color:
                    self._color = val
                    changed_properties[_PROP_COLOR] = val
                    validity_maybe_changed = True

        val = rprops.get(_PROP_TYPE, self._type)
        if isinstance(val, unicode):
            try:
                val = val.encode('ascii')
            except UnicodeError:
                _logger.debug('Invalid activity type %s', val)
            else:
                if _PROP_TYPE in changed or val != self._type:
                    if self._type:
                        _logger.debug('Peer attempted to change activity '
                                      'type from %s to %s: ignoring',
                                      self._type, val)
                    else:
                        self._type = val
                        changed_properties[_PROP_TYPE] = val
                        validity_maybe_changed = True

        # Set custom properties
        # FIXME: is this actually required? If so, it needs to go into
        # the PropertiesChanged dict somehow
        if len(cprops.keys()) > 0:
            self._custom_props = cprops

        if changed_properties:
            self.PropertiesChanged(changed_properties)

        if validity_maybe_changed:
            self._update_validity()

    def _split_properties(self, properties):
        """Extracts reserved properties.

        properties - Dictionary object containing properties keyed by
                     property names

        returns a tuple of 2 dictionaries, reserved properties and custom
        properties
        """
        rprops = {}
        cprops = {}
        for (key, value) in properties.items():
            if key in self._RESERVED_PROPNAMES:
                rprops[key] = value
            else:
                cprops[key] = value
        return (rprops, cprops)
