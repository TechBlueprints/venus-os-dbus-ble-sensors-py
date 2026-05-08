# Vendored: victron-ble

This directory contains a vendored copy of [keshavdv/victron-ble](https://github.com/keshavdv/victron-ble), version **0.9.3** (released to PyPI), with one local modification.

## Why vendored

The Cerbo GX has no convenient way to install Python packages from PyPI as part of a curl-based installer.  Vendoring is the simplest way to make `dbus-ble-sensors-py` work out of the box on a fresh Venus OS device for IP22 / Orion-TR / SolarSense BLE decoding.

## Local modification

`devices/base.py` has been patched to prefer Python's standard `cryptography` library over `PyCryptodome`.  The `cryptography` package ships with Venus OS as `python3-cryptography`; `PyCryptodome` does not.

The patch:

1. Wraps the `Crypto.*` imports in a `try/except ImportError` and adds a parallel `from cryptography.hazmat.primitives.ciphers import ...` block.
2. Adds a small `_aes_ctr_decrypt` helper that picks the available backend and produces byte-identical output between the two.
3. The single call site in `Device.decrypt_data` is rewritten to call `_aes_ctr_decrypt`.

If both libraries happen to be installed, `cryptography` is used.  If only `PyCryptodome` is installed, the upstream code path runs.  If neither is available, an explicit `ImportError` is raised at import time with a helpful message.

The patch was validated against PyCryptodome's reference implementation: for identical (key, iv, ciphertext) inputs both backends produce byte-identical plaintext.

## License

`victron-ble` is released under the [Unlicense](https://unlicense.org/) (public domain).  See `LICENSE` in this directory.

The local cryptography-backend patch is contributed under the same Unlicense terms.

## Note on upstream attributions

The vendored library contains its original third-party attributions, e.g. `devices/orion_xs.py` credits Fabian Schmidt for documenting the Orion XS protocol (see [Fabian-Schmidt/esphome-victron_ble#54](https://github.com/Fabian-Schmidt/esphome-victron_ble/pull/54)).  These credits are upstream authors' own crediting of *their* protocol-analysis work and are preserved verbatim out of respect for the original authors of the public domain library.
