# Copyright 2026 Clint Goudie-Nice
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
from ble_role import BleRole


class BleRoleAcLoad(BleRole):
    """AC load role — a device metering an AC consumer.

    Registers as ``com.victronenergy.acload.<dev_id>``, the Venus service
    type for AC load meters, so the reading shows up in the consumption
    view.  Devices claiming this role publish the standard AC paths
    (``/Ac/Power``, ``/Ac/L1/Current`` ...) themselves; the role carries
    no shared settings or alarms.
    """

    NAME = 'acload'

    def __init__(self, config: dict = None):
        super().__init__(config)

        self.info.update(
            {
                'name': 'acload',
                'dev_instance': 40,
                'settings': [],
            },
        )
