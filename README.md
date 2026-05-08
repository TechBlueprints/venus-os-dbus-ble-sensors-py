# Dbus BLE sensors py

> **This is a fork of [ldenisey/venus-os-dbus-ble-sensors-py](https://github.com/ldenisey/venus-os-dbus-ble-sensors-py) with the following PRs merged in ahead of upstream:**
> - [#2 Replace Bleak with raw HCI monitor channel](https://github.com/ldenisey/venus-os-dbus-ble-sensors-py/pull/2) — passive BLE scanning via `AdvertisementMonitor1`, no scan contention
> - [#3 Add SeeLevel 709-BTP3/BTP7 support](https://github.com/ldenisey/venus-os-dbus-ble-sensors-py/pull/3) — tank, temperature, and battery sensors
> - [#4 Add alarm delay setting to BleRoleTank](https://github.com/ldenisey/venus-os-dbus-ble-sensors-py/pull/4)
> - [#6 Fix Mopeka tank level scaling](https://github.com/ldenisey/venus-os-dbus-ble-sensors-py/pull/6) — execution order, butane formula, role consolidation
> - [#7 Cache D-Bus connections](https://github.com/ldenisey/venus-os-dbus-ble-sensors-py/pull/7) — prevent connection proliferation
> - [#8 Add curl-based install method](https://github.com/ldenisey/venus-os-dbus-ble-sensors-py/pull/8) — install without opkg or remounting the filesystem

Venus OS dbus service for BLE device support. Replaces and extends [official dbus ble service](https://github.com/victronenergy/dbus-ble-sensors/tree/master) which does not allow collaboration for new devices support.

Devices currently supported :
| Brand          | model                      | Product page                                                                                                   |
| -------------- | -------------------------- | -------------------------------------------------------------------------------------------------------------- |
| Teltonika      | EYE Sensor                 | https://www.teltonika-gps.com/products/accessories/sensors-beacons/eye-sensor-standard                         |
| Safiery        | Star Tank                  | https://safiery.com/product/tank-level-sensor-star-tank-phased-coherent-radar-battery/                         |
| Gobius         | Gobius C                   | https://gobiusc.com/fr/                                                                                        |
| Victron Energy | SolarSense 750             | https://www.victronenergy.com/upload/documents/Datasheet-SolarSense-750-EN.pdf                                 |
| Mopeka         | Mopeka Pro Check Universal | https://mopeka.com/consumer-solutions/#:~:text=Mopeka%20Pro%20Check%20Universal%20%E2%80%93%20Latest%20Version |
| Mopeka         | Mopeka Pro Check H2O       | https://mopeka.com/commercial-industry-based-solutions/water/#:~:text=Mopeka%20Pro%20Check                     |
| Mopeka         | Mopeka Pro Check LPG       | https://mopeka.com/consumer-solutions/#:~:text=Mopeka%20Pro%20Check,-Ideal%20for%20Residential                 |
| Mopeka         | Mopeka Pro 200             | https://mopeka.com/consumer-solutions/#:~:text=Mopeka%20Pro200                                                 |
| Mopeka         | Mopeka Pro Plus            | https://mopeka.com/consumer-solutions/#:~:text=Mopeka%20Pro%20Plus                                             |
| Mopeka         | Mopeka TD40                | https://mopeka.com/consumer-solutions/#:~:text=Mopeka%20TD40                                                   |
| Mopeka         | Mopeka TD200               | https://mopeka.com/commercial-industry-based-solutions/water/#:~:text=Mopeka%20TD40,%20TD200                   |
| Ruuvi          | Ruuvi Tag                  | https://ruuvi.com/ruuvitag/                                                                                    |
| Ruuvi          | Ruuvi Air                  | https://ruuvi.com/air/                                                                                         |
| Garnet         | SeeLevel 709-BTP3          | https://www.garnetinstruments.com/document/709-btp3-seelevel-ii-tank-monitor-2/                                |
| Garnet         | SeeLevel 709-BTP7          | https://www.garnetinstruments.com/document/709-btp7-seelevel-ii-tank-monitor/                                  |
| Victron Energy | Orion-TR Smart DC-DC       | https://www.victronenergy.com/dc-dc-converters/orion-tr-smart-dc-dc-charger-isolated                           |
| Victron Energy | Blue Smart IP22 Charger    | https://www.victronenergy.com/chargers/blue-smart-ip22-charger                                                 |

The two Victron chargers (Orion-TR Smart, Blue Smart IP22) are
**fully integrated chargers** from `dbus-systemcalc-py`'s point of
view — they participate in DVCC (`/Link/{ChargeCurrent,
ChargeVoltage, NetworkMode, NetworkStatus, *Sense, BatteryCurrent}`),
publish charger-side `/Alarms/*`, accumulate
`/History/Cumulative/User/*`, and persist user-set
`/Settings/{ChargeCurrentLimit, AbsorptionVoltage, FloatVoltage}`
to `com.victronenergy.settings`.  A real Victron BMS controls them
the same way it controls a USB-attached Phoenix Smart IP43.

Implementation notes:

- [`docs/IP22-INTEGRATION.md`](docs/IP22-INTEGRATION.md) — IP22
  driver, role, DVCC contract, alarm derivation, history accumulators
- [`docs/ORION-TR-INTEGRATION.md`](docs/ORION-TR-INTEGRATION.md) —
  Orion-TR driver, dcdc ↔ alternator role swap, integrated-charger
  surface on both roles
- `tests/` — self-contained pytest suite covering the shared
  infrastructure (`ble_charger_common`) and per-driver dispatch.
  Run via `./tests/run.sh` — no D-Bus, BlueZ, or hardware needed.
- `scripts/probe_charger_vregs.py` — VREG-discovery tool for
  extending support to new SKUs or firmware versions.

## Installation

> **Note:** This fork is installed via curl only.  The upstream opkg
> feed does not carry the changes in this fork, so `opkg install
> dbus-ble-sensors-py` will not work here.  See [PR #8](https://github.com/ldenisey/venus-os-dbus-ble-sensors-py/pull/8)
> for the rationale behind the curl-based installer.

Run this one-liner on your Venus OS device (SSH as root):

``` bash
curl -fsSL https://raw.githubusercontent.com/TechBlueprints/venus-os-dbus-ble-sensors-py/main/install.sh | bash
```

This installs to `/data/apps/dbus-ble-sensors-py/`, which persists across firmware updates automatically.  If an existing opkg-based installation is detected, it will be cleanly removed and replaced — all device settings are preserved.

To update, re-run the same command.

To disable:

``` bash
bash /data/apps/dbus-ble-sensors-py/disable.sh
```

To re-enable after disabling or a firmware update:

``` bash
bash /data/apps/dbus-ble-sensors-py/enable.sh
```

To fully remove:

``` bash
bash /data/apps/dbus-ble-sensors-py/disable.sh
rm -rf /data/apps/dbus-ble-sensors-py
```

## Usage

Device scan and enabling is done through the GUI, as described in the official documentations, i.e. [Cerbo GX bluetooth](https://www.victronenergy.com/media/pg/Cerbo_GX/en/connecting-supported-non-victron-products.html#UUID-8def9c4a-f36e-7048-1b4f-7294538eb31b).  
In short devices can be enabled/disabled in *Settings* -> *Integrations* -> *Bluetooth Sensors* and configured in *Settings* -> *Devices* dedicated menu.

> [!NOTE]  
> Even though the configuration process is the same, the configuration themselves are NOT shared between this service and official ble service
> hence configuration will have to be reset when switching between the two.

## Development

For technical info and guide to add new devices, see [dedicated developer page](DEVELOPMENT.md).
