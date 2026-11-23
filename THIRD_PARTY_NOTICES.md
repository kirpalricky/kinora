# Third-party notices

Kinora itself is MIT-licensed (see `LICENSE`). It depends on the packages below. Versions are those installed in the development environment when this file was written (2026-11-23); licence data comes from each package's installed metadata. Re-check after upgrading dependencies.

## Licence flags (read first)

- **python-kasa 0.12.0 is GPL-3.0-or-later (copyleft, NOT permissive).** It is a transitive dependency of `pytapo` (pytapo requires it). Kinora does not bundle or copy it: it is installed separately by pip, and Kinora's own code does not import it directly. Whether a GPL dependency installed alongside an MIT application affects the application's licence when distributed together is a legal question; do not ship python-kasa inside a bundled installer (PyInstaller, zip, container image) without taking advice, and keep the pip-install-from-PyPI route as the way users get it.
- **certifi 2026.11.20 is MPL-2.0 (weak, file-level copyleft).** Unmodified use as a dependency is fine; it is not modified or bundled by Kinora.
- **ffmpeg / ffprobe are external programs** (see the end of this file); their licence is LGPL or GPL depending on the build the user installs.
- Everything else is permissive (MIT, BSD, Apache-2.0, PSF-2.0, MIT-0).

## Bundled in the repository

| Name | Version | Licence | Project URL |
|------|---------|---------|-------------|
| hls.js (`static/hls.min.js`) | 1.5.17 (from the version string in the file) | Apache-2.0 (upstream licence text in `licenses/hls.js-LICENSE.txt`) | https://github.com/video-dev/hls.js |

The upstream hls.js `LICENSE` file (v1.5.17) is included as `licenses/hls.js-LICENSE.txt`.

## Direct runtime dependencies (`requirements.txt`)

| Name | Version | Licence | Project URL |
|------|---------|---------|-------------|
| aiohttp | 3.14.5 | Apache-2.0 AND MIT | https://github.com/aio-libs/aiohttp |
| pytapo | 3.4.34 | MIT | https://github.com/JurajNyiri/pytapo |
| platformdirs | 4.14.8 | MIT | https://github.com/tox-dev/platformdirs |
| keyring | 25.7.0 | MIT | https://github.com/jaraco/keyring |
| psutil | 7.2.2 | BSD-3-Clause | https://github.com/giampaolo/psutil |

## Transitive dependencies (installed in `.venv`)

| Name | Version | Licence | Project URL |
|------|---------|---------|-------------|
| **python-kasa** | 0.12.0 | **GPL-3.0-or-later (copyleft)** | https://github.com/python-kasa/python-kasa |
| **certifi** | 2026.11.20 | **MPL-2.0 (weak copyleft)** | https://github.com/certifi/python-certifi |
| aiofiles | 25.1.0 | Apache-2.0 | https://github.com/Tinche/aiofiles |
| aiohappyeyeballs | 2.7.1 | PSF-2.0 | https://github.com/aio-libs/aiohappyeyeballs |
| aiosignal | 1.4.0 | Apache-2.0 | https://github.com/aio-libs/aiosignal |
| asyncclick | 8.4.2.1 | BSD-3-Clause | https://github.com/python-trio/asyncclick/ |
| attrs | 26.1.0 | MIT | https://github.com/python-attrs/attrs |
| cffi | 2.1.1 | MIT-0 | https://github.com/python-cffi/cffi |
| charset-normalizer | 3.5.2 | MIT | https://github.com/jawah/charset_normalizer |
| cryptography | 50.0.2 | Apache-2.0 OR BSD-3-Clause | https://github.com/pyca/cryptography |
| frozenlist | 1.8.0 | Apache-2.0 | https://github.com/aio-libs/frozenlist |
| idna | 3.20 | BSD-3-Clause | https://github.com/kjd/idna |
| jaraco.classes | 3.4.0 | MIT | https://github.com/jaraco/jaraco.classes |
| jaraco.context | 6.1.2 | MIT | https://github.com/jaraco/jaraco.context |
| jaraco.functools | 4.6.0 | MIT | https://github.com/jaraco/jaraco.functools |
| mashumaro | 3.23 | Apache-2.0 | https://mashumaro.io |
| more-itertools | 11.2.1 | MIT | https://github.com/more-itertools/more-itertools |
| multidict | 7.2.1 | Apache-2.0 | https://github.com/aio-libs/multidict |
| propcache | 0.5.4 | Apache-2.0 | https://github.com/aio-libs/propcache |
| pycparser | 3.12 | BSD-3-Clause | https://github.com/eliben/pycparser |
| pycryptodome | 4.5.0 | BSD-2-Clause AND Unlicense AND MIT | https://www.pycryptodome.org |
| requests | 2.34.2 | Apache-2.0 | https://github.com/psf/requests |
| rtp | 0.0.4 | Apache-2.0 | https://github.com/bbc/rd-apmm-python-lib-rtp |
| typing_extensions | 4.16.0 | PSF-2.0 | https://github.com/python/typing_extensions |
| urllib3 | 2.8.0 | MIT | https://github.com/urllib3/urllib3 |
| yarl | 1.25.1 | Apache-2.0 | https://github.com/aio-libs/yarl |

(`pip` 26.2.2, MIT, is present in the environment but is a tool, not a runtime dependency. Project URLs for attrs, charset-normalizer and urllib3 are their well-known upstream repositories, as their metadata carries no home-page field.)

## External programs, not bundled

Kinora calls **ffmpeg** and **ffprobe** as separate programs found on the user's `PATH`. They are not included in the repository or in a pip/pipx install. Their licence depends on how the user's copy was built: LGPL-2.1-or-later for LGPL builds, GPL-2.0-or-later (or GPL-3.0) for builds with GPL components. Because Kinora runs them as separate processes and does not link them, Kinora's MIT licence is unaffected; if you ever redistribute ffmpeg binaries with Kinora, you take on that build's licence obligations (including source offers). See https://ffmpeg.org/legal.html.

## Trademarks

TP-Link and Tapo are trademarks of their respective owner. They are used only to describe which cameras Kinora is compatible with. Kinora is unofficial software and is not affiliated with or endorsed by TP-Link.
