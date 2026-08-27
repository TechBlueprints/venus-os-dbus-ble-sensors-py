from __future__ import annotations
import dbus
import logging
from dbus_bus import get_bus
from vedbus import VeDbusItemImport, VeDbusItemExport

class DbusSettingsService(object):
    """
    Inspired from SettingsDevice class of settingsdevice.py file of velib_python, but :
    - removing the use of arbitrary setting names, using paths instead
    - allowing reading settings
    - allowing different callbacks for each settings
    - providing proxy item creation helper methods
    """

    _SETTINGS_SERVICENAME = 'com.victronenergy.settings'

    def __init__(self):
        self._bus: dbus.bus.BusConnection = get_bus(self._SETTINGS_SERVICENAME)
        self._paths = {}
        if self._SETTINGS_SERVICENAME not in self._bus.list_names():
            raise Exception(f"Dbus service {self._SETTINGS_SERVICENAME!r} does not exist.")

    def get_item(self, path: str, def_value: object = None, min_value: int = 0, max_value: int = 0) -> VeDbusItemImport:
        # Get the setting item, initializing it only if it does not exists and if a default value is given
        if (item := self._paths.get(path, None)) is not None:
            return item

        # Probe existence with a non-subscribing import.  Each
        # ``VeDbusItemImport`` constructed with ``createsignal=True``
        # (the default) installs a D-Bus match rule that is owned by
        # the connection, not by the Python object — once the rule is
        # in place the daemon counts it against the per-connection
        # ``max_match_rules_per_connection`` limit (1024 by default)
        # even after the import is garbage-collected.  Probe without
        # subscribing so a single long-lived signal-bearing import is
        # the only rule we add per cache miss.
        probe = VeDbusItemImport(self._bus, self._SETTINGS_SERVICENAME, path, createsignal=False)

        if not probe.exists:
            if def_value is None:
                # Nothing to bind a subscription to.  Cache the probe so
                # repeated lookups don't re-probe; callers that depend on
                # ``.exists`` will see False.
                self._paths[path] = probe
                return probe
            return self._add_and_subscribe(path, def_value, min_value, max_value, silent=False, callback=None)

        # Setting exists; bind exactly one signal-bearing import.
        item = VeDbusItemImport(self._bus, self._SETTINGS_SERVICENAME, path)
        self._paths[path] = item
        return item

    def get_value(self, path) -> object:  # int, float, str, None
        return self.get_item(path).get_value()

    def try_get_value(self, path: str):
        """Return setting value, or None if the path does not exist.

        Routed through :meth:`get_item` so the import is cached.  A fresh
        ``VeDbusItemImport`` per call leaks a D-Bus match rule every
        time: ``bus.get_object`` on a well-known name installs a
        ``NameOwnerChanged`` watch keyed to the resolved unique name, and
        that rule belongs to the CONNECTION, not to the Python object —
        so it outlives the import and counts against
        ``max_match_rules_per_connection`` (1024).  ``createsignal=False``
        suppresses the PropertiesChanged rule and does nothing about
        this one, which is what made the leak invisible to the reasoning
        already written above.

        Measured on the prod Cerbo before this: 435 identical copies of
        that rule on one connection in ~40 minutes, one every ~5.5s,
        against a ceiling of 1024.

        ``get_item`` with no default never creates a setting, so this
        stays a read.  Its cached import is signal-bearing when the path
        exists, which also fixes a staleness trap: a
        ``createsignal=False`` import has no subscription, so its
        ``get_value`` would return whatever was true at construction
        forever.
        """
        item = self.get_item(path)
        if not item.exists:
            return None
        return item.get_value()

    def list_device_settings(self) -> list:
        """Every ``/Settings/Devices/<id>/...`` key currently stored.

        Used to tell a device we have configured before from one we are
        merely hearing.  Returns relative keys such as
        ``orion_tr_c36eed421ff2/charger/Enabled``.
        """
        try:
            item = VeDbusItemImport(
                self._bus, self._SETTINGS_SERVICENAME, "/Settings/Devices",
                createsignal=False)
            value = item.get_value()
        except Exception:
            logging.exception("could not enumerate /Settings/Devices")
            return []
        if not isinstance(value, dict):
            return []
        return [str(k) for k in value.keys()]

    def set_item(self, path: str, def_value: object = None, min_value: int = 0, max_value: int = 0, silent=False, callback=None) -> VeDbusItemImport:
        # Probe existence and current attributes without subscribing — see
        # the ``get_item`` comment for why we cannot afford a throwaway
        # signal-bearing import here.
        probe = VeDbusItemImport(self._bus, self._SETTINGS_SERVICENAME, path, createsignal=False)

        if probe.exists and (def_value, min_value, max_value, silent) == probe._proxy.GetAttributes():
            item = VeDbusItemImport(self._bus, self._SETTINGS_SERVICENAME, path, callback)
            self._paths[path] = item
            return item

        return self._add_and_subscribe(path, def_value, min_value, max_value, silent, callback)

    def _add_and_subscribe(self, path: str, def_value: object, min_value: int,
                           max_value: int, silent: bool, callback) -> VeDbusItemImport:
        """Call ``AddSetting`` (or ``AddSilentSetting``) on
        ``com.victronenergy.settings`` for *path*, then bind a single
        signal-bearing import.  The ``/Settings`` parent probe used to
        invoke the method is constructed with ``createsignal=False`` and
        immediately discarded — only one match rule is added per call.
        """
        if isinstance(def_value, (int, dbus.Int64)):
            itemType = 'i'
        elif isinstance(def_value, float):
            itemType = 'f'
        else:
            itemType = 's'

        parent_probe = VeDbusItemImport(
            self._bus, self._SETTINGS_SERVICENAME, '/Settings', createsignal=False)
        setting_path = path.replace('/Settings/', '', 1)
        if silent:
            parent_probe._proxy.AddSilentSetting('', setting_path, def_value, itemType, min_value, max_value)
        else:
            parent_probe._proxy.AddSetting('', setting_path, def_value, itemType, min_value, max_value)

        busitem = VeDbusItemImport(self._bus, self._SETTINGS_SERVICENAME, path, callback)
        self._paths[path] = busitem
        return busitem

    def set_value(self, path, new_value):
        if (setting := self._paths.get(path, None)) is None:
            logging.error(f"Can not set value of non-existing {path!r} to {new_value!r}.")
        else:
            if (result := setting.set_value(new_value)) != 0:
                logging.error(f"Failed to set setting {path!r} to {new_value!r}, result={result}.")

    def set_proxy_callback(self, setting_path: str, remote_item: VeDbusItemExport,
                           on_change=None):
        """Bridge ``setting_path`` on com.victronenergy.settings into the
        local ``remote_item``.  When the upstream setting changes, the
        local item is updated via ``local_set_value`` (silent — no
        D-Bus signal of our own).

        ``on_change``, if provided, is called with the new value
        *after* the local item is updated.  This is the hook for
        consumers that want to react to settings changes (e.g. GUI
        toggles) without having to poll.  The local item's
        ``_onchangecallback`` only fires when EXTERNAL D-Bus clients
        write to OUR path, so it's the wrong hook for "GUI toggled
        the setting in the settings service" — this is.
        """
        def _callback(service_name, change_path, changes):
            if service_name != DbusSettingsService._SETTINGS_SERVICENAME or change_path != setting_path:
                return
            new_value = changes['Value']
            if new_value != remote_item.local_get_value():
                remote_item.local_set_value(new_value)
            if on_change is not None:
                try:
                    on_change(new_value)
                except Exception:
                    logging.exception(
                        f"set_proxy_callback on_change for {setting_path!r} raised")
        self.get_item(setting_path).eventCallback = _callback

    def unset_proxy_callback(self, setting_path: str):
        self.get_item(setting_path).eventCallback = None

    def __getitem__(self, path):
        return self.get_value(path)

    def __setitem__(self, path, new_value):
        self.set_value(path, new_value)
