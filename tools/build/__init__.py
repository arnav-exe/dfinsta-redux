"""APK assembly: apktool build of the changed DEX files, then a stock graft.

Named `port_430` until 2026-08-23, which had stopped being true. `build.py` and
`prepare_tree.py` are general and are what every port runs — `driver.py` pins
`build.py` as its `BUILDER` and shells out to it with per-version arguments, and
`prepare_tree.py` handles manifest shapes as new as Instagram 440's `<queries>`
provider. Only `verify_apk_430.py` is still 430-specific, and it now says so in
its name: it pins descriptors that moved in 439, and the driver does not use it
(it passes `--verifier generic`, which routes to `tools/verify/verify_build.py`).
"""
