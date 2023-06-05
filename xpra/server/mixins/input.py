# -*- coding: utf-8 -*-
# This file is part of Xpra.
# Copyright (C) 2010-2023 Antoine Martin <antoine@xpra.org>
# Copyright (C) 2008 Nathaniel Smith <njs@pobox.com>
# Xpra is released under the terms of the GNU GPL v2, or, at your option, any
# later version. See the file COPYING for details.
#pylint: disable-msg=E1101

from time import monotonic
from typing import List, Dict, Any

from xpra.os_util import bytestostr
from xpra.util import typedict, net_utf8, envbool
from xpra.server.mixins.stub_server_mixin import StubServerMixin
from xpra.log import Logger

keylog = Logger("keyboard")
mouselog = Logger("mouse")

INPUT_SEQ_NO = envbool("XPRA_INPUT_SEQ_NO", False)


class InputServer(StubServerMixin):
    """
    Mixin for servers that handle input devices
    (keyboard, mouse, etc)
    """

    def __init__(self):
        self.input_devices = "auto"
        self.input_devices_format = None
        self.input_devices_data = None
        self.pointer_sequence = {}

        self.mod_meanings = {}
        self.keyboard_config = None
        self.keymap_changing_timer = 0  #to ignore events when we know we are changing the configuration
        self.key_repeat = None
        #ugly: we're duplicating the value pair from "key_repeat" here:
        self.key_repeat_delay = -1
        self.key_repeat_interval = -1
        #store list of currently pressed keys
        #(using a dict only so we can display their names in debug messages)
        self.keys_pressed : Dict[int,str] = {}
        self.keys_timedout : Dict[int,float] = {}
        #timers for cancelling key repeat when we get jitter
        self.key_repeat_timer = 0

        self.last_mouse_user = None

    def init(self, opts) -> None:
        props = typedict()
        keymap = props.setdefault("keymap", {})
        for option in ("sync", "layout", "layouts", "variant", "variants", "options"):
            v = getattr(opts, f"keyboard_{option}", None)
            if v is not None:
                keymap[option] = v
        self.keyboard_config = self.get_keyboard_config(props)

    def setup(self) -> None:
        self.watch_keymap_changes()

    def cleanup(self) -> None:
        self.clear_keys_pressed()
        self.keyboard_config = None

    def reset_focus(self) -> None:
        self.clear_keys_pressed()

    def last_client_exited(self) -> None:
        self.clear_keys_pressed()

    def get_info(self, _proto) -> Dict[str,Any]:
        return {"keyboard" : self.get_keyboard_info()}

    def get_server_features(self, _source=None) -> Dict[str,Any]:
        return {
            "input-devices"         : self.input_devices,
            "pointer.relative"      : True,
            }

    def get_caps(self, _source) -> Dict[str,Any]:
        if not self.key_repeat:
            return {}
        return {
            "key_repeat"           : self.key_repeat,
            "key_repeat_modifiers" : True,
            }

    def parse_hello(self, ss, caps:typedict, send_ui:bool) -> None:
        if send_ui:
            self.parse_hello_ui_keyboard(ss, caps)

    def watch_keymap_changes(self) -> None:
        """ GTK servers will start listening for the 'keys-changed' signal """

    def parse_hello_ui_keyboard(self, ss, c:typedict) -> None:
        other_ui_clients : List[str] = [s.uuid for s in self._server_sources.values() if s!=ss and s.ui_client]
        #parse client config:
        ss.keyboard_config = self.get_keyboard_config(c)    #pylint: disable=assignment-from-none

        if not other_ui_clients:
            #so only activate this feature afterwards:
            self.key_repeat = c.intpair("key_repeat") or (0, 0)
            self.set_keyboard_repeat(self.key_repeat)
            #always clear modifiers before setting a new keymap
            ss.make_keymask_match(c.strtupleget("modifiers"))
        else:
            self.set_keyboard_repeat(None)
            self.key_repeat = (0, 0)
        self.key_repeat_delay, self.key_repeat_interval = self.key_repeat
        self.set_keymap(ss)

    def get_keyboard_info(self) -> Dict[str,Any]:
        start = monotonic()
        info = {
             "repeat"           : {
                                   "delay"      : self.key_repeat_delay,
                                   "interval"   : self.key_repeat_interval,
                                   },
             "keys_pressed"     : tuple(self.keys_pressed.values()),
             "modifiers"        : self.mod_meanings,
             }
        kc = self.keyboard_config
        if kc:
            info.update(kc.get_info())
        keylog("get_keyboard_info took %ims", (monotonic()-start)*1000)
        return info


    def _process_layout(self, proto, packet) -> None:
        if self.readonly:
            return
        layout, variant = packet[1:3]
        if len(packet)>=4:
            options = packet[3]
        else:
            options = ""
        ss = self.get_server_source(proto)
        if ss and ss.set_layout(layout, variant, options):
            self.set_keymap(ss, force=True)

    def _process_keymap(self, proto, packet) -> None:
        if self.readonly:
            return
        props = typedict(packet[1])
        ss = self.get_server_source(proto)
        if ss is None:
            return
        keylog("received new keymap from client")
        other_ui_clients = [s.uuid for s in self._server_sources.values() if s!=ss and s.ui_client]
        if other_ui_clients:
            keylog.warn("Warning: ignoring keymap change as there are %i other clients", len(other_ui_clients))
            return
        kc = ss.keyboard_config
        if kc and kc.enabled:
            kc.parse_options(props)
            self.set_keymap(ss, True)
        modifiers = props.get("modifiers", [])
        ss.make_keymask_match(modifiers)

    def set_keyboard_layout_group(self, grp:int) -> None:
        #only actually implemented in X11ServerBase
        pass

    def _process_key_action(self, proto, packet) -> None:
        if self.readonly:
            return
        wid, keyname, pressed, modifiers, keyval, keystr, client_keycode, group = packet[1:9]
        ss = self.get_server_source(proto)
        if ss is None:
            return
        keyname = net_utf8(keyname)
        keystr = net_utf8(keystr)
        modifiers = list(net_utf8(x) for x in modifiers)
        self.set_ui_driver(ss)
        keycode, group = self.get_keycode(ss, client_keycode, keyname, pressed, modifiers, keyval, keystr, group)
        keylog("process_key_action(%s) server keycode=%s, group=%i", packet, keycode, group)
        if group>=0 and keycode>=0:
            self.set_keyboard_layout_group(group)
        #currently unused: (group, is_modifier) = packet[8:10]
        self._focus(ss, wid, None)
        ss.make_keymask_match(modifiers, keycode, ignored_modifier_keynames=[keyname])
        #negative keycodes are used for key events without a real keypress/unpress
        #for example, used by win32 to send Caps_Lock/Num_Lock changes
        if keycode>=0:
            try:
                is_mod = ss.is_modifier(keyname, keycode)
                self._handle_key(wid, pressed, keyname, keyval, keycode, modifiers, is_mod, ss.keyboard_config.sync)
            except Exception as e:
                keylog("process_key_action%s", (proto, packet), exc_info=True)
                keylog.error("Error: failed to %s key", ["unpress", "press"][pressed])
                keylog.estr(e)
                keylog.error(" for keyname=%s, keyval=%i, keycode=%i", keyname, keyval, keycode)
        ss.user_event()

    def get_keycode(self, ss, client_keycode:int, keyname:str,
                    pressed:bool, modifiers:List, keyval:int, keystr:str, group:int):
        return ss.get_keycode(client_keycode, keyname, pressed, modifiers, keyval, keystr, group)

    def fake_key(self, keycode, press):
        keylog("fake_key%s is not implemented", (keycode, press))

    def _handle_key(self, wid:int, pressed:bool, name:str, keyval:int, keycode:int, modifiers:List, is_mod:bool=False, sync:bool=True):
        """
            Does the actual press/unpress for keys
            Either from a packet (_process_key_action) or timeout (_key_repeat_timeout)
        """
        keylog("handle_key(%s)", (wid, pressed, name, keyval, keycode, modifiers, is_mod, sync))
        if pressed and (wid is not None) and (wid not in self._id_to_window):
            keylog("window %s is gone, ignoring key press", wid)
            return
        if keycode<0:
            keylog.warn("ignoring invalid keycode=%s", keycode)
            return
        if keycode in self.keys_timedout:
            del self.keys_timedout[keycode]
        def press():
            keylog("handle keycode pressing   %3i: key '%s'", keycode, name)
            self.keys_pressed[keycode] = name
            self.fake_key(keycode, True)
        def unpress():
            keylog("handle keycode unpressing %3i: key '%s'", keycode, name)
            if keycode in self.keys_pressed:
                del self.keys_pressed[keycode]
            self.fake_key(keycode, False)
        if pressed:
            if keycode not in self.keys_pressed:
                press()
                if not sync and not is_mod:
                    #keyboard is not synced: client manages repeat so unpress
                    #it immediately unless this is a modifier key
                    #(as modifiers are synced via many packets: key, focus and mouse events)
                    unpress()
            else:
                keylog("handle keycode %s: key %s was already pressed, ignoring", keycode, name)
        else:
            if keycode in self.keys_pressed:
                unpress()
            else:
                keylog("handle keycode %s: key %s was already unpressed, ignoring", keycode, name)
        if not is_mod and sync and self.key_repeat_delay>0 and self.key_repeat_interval>0:
            self._key_repeat(wid, pressed, name, keyval, keycode, modifiers, is_mod, self.key_repeat_delay)

    def cancel_key_repeat_timer(self) -> None:
        krt = self.key_repeat_timer
        if krt:
            self.key_repeat_timer = 0
            self.source_remove(krt)

    def _key_repeat(self, wid:int, pressed:bool, keyname:str, keyval:int, keycode:int,
                    modifiers:List, is_mod:bool, delay_ms:int=0) -> None:
        """ Schedules/cancels the key repeat timeouts """
        self.cancel_key_repeat_timer()
        if pressed:
            delay_ms = min(1500, max(250, delay_ms))
            keylog("scheduling key repeat timer with delay %s for %s / %s", delay_ms, keyname, keycode)
            now = monotonic()
            self.key_repeat_timer = self.timeout_add(delay_ms, self._key_repeat_timeout,
                                                     now, delay_ms, wid, keyname, keyval, keycode, modifiers, is_mod)

    def _key_repeat_timeout(self, when, delay_ms:int, wid:int, keyname:str, keyval:int, keycode:int,
                            modifiers:List, is_mod:bool) -> None:
        self.key_repeat_timer = 0
        now = monotonic()
        keylog("key repeat timeout for %s / '%s' - clearing it, now=%s, scheduled at %s with delay=%s",
               keyname, keycode, now, when, delay_ms)
        self._handle_key(wid, False, keyname, keyval, keycode, modifiers, is_mod, True)
        self.keys_timedout[keycode] = now

    def _process_key_repeat(self, proto, packet) -> None:
        if self.readonly:
            return
        ss = self.get_server_source(proto)
        if ss is None:
            return
        wid, keyname, keyval, client_keycode, modifiers = packet[1:6]
        keyname = bytestostr(keyname)
        modifiers = tuple(bytestostr(x) for x in modifiers)
        group = 0
        if len(packet)>=7:
            group = packet[6]
        keystr = ""
        keycode, group = ss.get_keycode(client_keycode, keyname, modifiers, keyval, keystr, group)
        if group>=0:
            self.set_keyboard_layout_group(group)
        #key repeat uses modifiers from a pointer event, so ignore mod_pointermissing:
        ss.make_keymask_match(modifiers)
        if not ss.keyboard_config.sync:
            #this check should be redundant: clients should not send key-repeat without
            #having keyboard_sync enabled
            return
        if keycode not in self.keys_pressed:
            #the key is no longer pressed, has it timed out?
            when_timedout = self.keys_timedout.get(keycode, None)
            if when_timedout:
                del self.keys_timedout[keycode]
            now = monotonic()
            if when_timedout and (now-when_timedout)<30:
                #not so long ago, just re-press it now:
                keylog("key %s/%s, had timed out, re-pressing it", keycode, keyname)
                self.keys_pressed[keycode] = keyname
                self.fake_key(keycode, True)
        is_mod = ss.is_modifier(keyname, keycode)
        self._key_repeat(wid, True, keyname, keyval, keycode, modifiers, is_mod, self.key_repeat_interval)
        ss.user_event()

    def _process_keyboard_sync_enabled_status(self, proto, packet) -> None:
        if self.readonly:
            return
        ss = self.get_server_source(proto)
        if ss is None:
            return
        kc = ss.keyboard_config
        if kc:
            kc.sync = bool(packet[1])
            keylog("toggled keyboard-sync to %s for %s", kc.sync, ss)

    def _keys_changed(self) -> None:
        keylog("input server: the keymap has been changed, keymap_changing_timer=%s", self.keymap_changing_timer)
        if not self.keymap_changing_timer:
            for ss in self._server_sources.values():
                if hasattr(ss, "keys_changed"):
                    ss.keys_changed()

    def clear_keys_pressed(self) -> None:
        keylog("clear_keys_pressed() is not implemented")

    def get_keyboard_config(self, props=None) -> None:
        keylog("get_keyboard_config(%s) is not implemented", props)
        return None

    def set_keyboard_repeat(self, key_repeat) -> None:
        keylog("set_keyboard_repeat(%s)", key_repeat)

    def set_keymap(self, ss, force:bool=False) -> None:
        keylog("set_keymap(%s, %s)", ss, force)


    ######################################################################
    # pointer:
    def _move_pointer(self, device_id, wid, pos, *args) -> None:
        raise NotImplementedError()

    def _adjust_pointer(self, proto, device_id, wid, pointer):
        #the window may not be mapped at the same location by the client:
        ss = self.get_server_source(proto)
        window = self._id_to_window.get(wid)
        if ss and window:
            ws = ss.get_window_source(wid)
            if ws:
                mapped_at = ws.mapped_at
                pos = self.get_window_position(window)
                if mapped_at and pos:
                    wx, wy = pos
                    cx, cy = mapped_at[:2]
                    if wx!=cx or wy!=cy:
                        dx, dy = wx-cx, wy-cy
                        if dx!=0 or dy!=0:
                            px, py = pointer[:2]
                            ax, ay = px+dx, py+dy
                            mouselog("client %2i: server window position: %12s, client window position: %24s, pointer=%s, adjusted: %s",
                                     ss.counter, pos, mapped_at, pointer, (ax, ay))
                            return [ax, ay]+list(pointer[2:])
        return pointer

    def _process_mouse_common(self, proto, device_id:int, wid:int, opointer, props=None):
        pointer = self._adjust_pointer(proto, device_id, wid, opointer)
        if not pointer:
            return None
        if self.do_process_mouse_common(proto, device_id, wid, pointer, props):
            return pointer
        return None

    def do_process_mouse_common(self, proto, device_id:int, wid:int, pointer, props) -> bool:
        return True

    def _process_pointer_button(self, proto, packet) -> None:
        mouselog("process_pointer_button(%s, %s)", proto, packet)
        if self.readonly:
            return
        ss = self.get_server_source(proto)
        if ss is None:
            return
        ss.user_event()
        self.last_mouse_user = ss.uuid
        self.set_ui_driver(ss)
        device_id, seq, wid, button, pressed, pointer, props = packet[1:8]
        if device_id>=0:
            #highest_seq = self.pointer_sequence.get(device_id, 0)
            #if INPUT_SEQ_NO and 0<=seq<=highest_seq:
            #    mouselog(f"dropped outdated sequence {seq}, latest is {highest_seq}")
            #    return
            self.pointer_sequence[device_id] = seq
        self.do_process_button_action(proto, device_id, wid, button, pressed, pointer, props)

    def _process_button_action(self, proto, packet) -> None:
        mouselog("process_button_action(%s, %s)", proto, packet)
        if self.readonly:
            return
        ss = self.get_server_source(proto)
        if ss is None:
            return
        ss.user_event()
        self.last_mouse_user = ss.uuid
        self.set_ui_driver(ss)
        wid, button, pressed, pointer, modifiers = packet[1:6]
        device_id = 0
        props = {
            "modifiers" : modifiers,
            }
        if len(packet)>=7:
            props["buttons"] = 6
        self.do_process_button_action(proto, device_id, wid, button, pressed, pointer, props)

    def do_process_button_action(self, proto, device_id, wid, button, pressed, pointer, props) -> None:
        """ all servers should implement this method """


    def _update_modifiers(self, proto, wid, modifiers) -> None:
        """ servers subclasses may change the modifiers state """

    def _process_pointer(self, proto, packet) -> None:
        #v5 packet format
        mouselog("_process_pointer(%s, %s) readonly=%s, ui_driver=%s",
                 proto, packet, self.readonly, self.ui_driver)
        if self.readonly:
            return
        ss = self.get_server_source(proto)
        if ss is None:
            return
        device_id, seq, wid, pdata, props = packet[1:6]
        if device_id>=0:
            highest_seq = self.pointer_sequence.get(device_id, 0)
            if INPUT_SEQ_NO and 0<=seq<=highest_seq:
                mouselog(f"dropped outdated sequence {seq}, latest is {highest_seq}")
                return
            self.pointer_sequence[device_id] = seq
        pointer = pdata[:2]
        if ss.pointer_relative and len(pdata)>=4:
            ss.mouse_last_relative_position = pdata[2:4]
        ss.mouse_last_position = pointer
        if self.ui_driver and self.ui_driver!=ss.uuid:
            return
        ss.user_event()
        self.last_mouse_user = ss.uuid
        if self._process_mouse_common(proto, device_id, wid, pdata, props):
            modifiers = props.get("modifiers")
            if modifiers is not None:
                self._update_modifiers(proto, wid, modifiers)


    def _process_pointer_position(self, proto, packet) -> None:
        mouselog("_process_pointer_position(%s, %s) readonly=%s, ui_driver=%s",
                 proto, packet, self.readonly, self.ui_driver)
        if self.readonly:
            return
        ss = self.get_server_source(proto)
        if ss is None:
            return
        wid, pdata, modifiers = packet[1:4]
        pointer = pdata[:2]
        if ss.pointer_relative and len(pdata)>=4:
            ss.mouse_last_relative_position = pdata[2:4]
        ss.mouse_last_position = pointer
        if self.ui_driver and self.ui_driver!=ss.uuid:
            return
        ss.user_event()
        self.last_mouse_user = ss.uuid
        props : Dict[str,Any] = {}
        device_id = -1
        if len(packet)>=6:
            device_id = packet[5]
        if self._process_mouse_common(proto, device_id, wid, pdata, props):
            self._update_modifiers(proto, wid, modifiers)


    ######################################################################
    # input devices:
    def _process_input_devices(self, _proto, packet) -> None:
        self.input_devices_format = packet[1]
        self.input_devices_data = packet[2]
        from xpra.util import print_nested_dict
        mouselog("client %s input devices:", self.input_devices_format)
        print_nested_dict(self.input_devices_data, print_fn=mouselog)
        self.setup_input_devices()

    def setup_input_devices(self) -> None:
        """
        subclasses can override this method
        the x11 servers use this to map devices
        """


    def init_packet_handlers(self) -> None:
        self.add_packet_handlers({
            #keyboard:
            "set-keyboard-sync-enabled" : self._process_keyboard_sync_enabled_status,
            "key-action"                : self._process_key_action,
            "key-repeat"                : self._process_key_repeat,
            "layout-changed"            : self._process_layout,
            "keymap-changed"            : self._process_keymap,
            #mouse:
            "pointer-button"            : self._process_pointer_button,     #v5
            "button-action"             : self._process_button_action,      #pre v5
            "pointer"                   : self._process_pointer,            #v5
            "pointer-position"          : self._process_pointer_position,   #pre v5
            #setup:
            "input-devices"             : self._process_input_devices,
            })
