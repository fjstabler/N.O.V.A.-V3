# A built panel APK, checked in

A binary in git is a bad habit, and this one is here for a specific reason:
installing an Android app normally means `adb` on a laptop, and the whole point
of this setup is a server that does not depend on a laptop being involved.

With the APK in the repository, the machine running the core can hand it to the
panel directly:

```sh
cd /opt/nova && git pull
cd apps/panel/prebuilt && python3 -m http.server 8000
```

Then open `http://<core-ip>:8000/nova-panel.apk` in the panel's own browser,
and stop the server with Ctrl-C once it has downloaded.

## It should not live here forever

Every rebuild committed here adds another few megabytes to the history that git
can never reclaim. Two better sources, once either is available:

- **CI** builds `nova-panel-debug-apk` on every run — see `.github/workflows/ci.yml`.
  It only runs on `main` and pull requests, so a feature branch produces nothing.
- **Locally**, `cd apps/panel && ./gradlew assembleDebug`, which needs a JDK and
  the Android SDK.

When one of those is set up, delete this directory and drop the `!` exception
from `.gitignore`.

## About the signature

Signed with the standard Android debug key, so it installs by sideloading but
will not upgrade over an APK signed by anything else — `adb uninstall
com.nova.panel` first if you ever swap sources.
