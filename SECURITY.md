# Security

Two Bells runs on your home network and holds API keys and a settings password, so security reports are welcome and taken seriously.

## Reporting a vulnerability

Please report it privately, not in a public issue: on GitHub, open the **Security** tab of [acornelissen/twobells](https://github.com/acornelissen/twobells) and choose **Report a vulnerability**. Say what you found, how to reproduce it, and what it lets someone do.

This is a one-person project, so replies are best effort: expect an acknowledgement within a week. Once a fix is out, the advisory credits you unless you'd rather it didn't.

## Supported versions

Fixes go into the latest release only. Upgrade to it before reporting, if you can.

## What's in scope

- The web server: the dashboard, its API and the settings site on port 8090, including sign-in, the password, sessions and the home-network checks.
- The setup wizard and the fetch service, and anything that reaches the internet on the app's behalf.
- How keys and the password are stored, and anything that could leak them (logs, pages, the container image, release files).
- The Pi's install, update and upgrade scripts, the systemd units and the network guard; the Docker image and compose files.
- The release pipeline: signatures, checksums and the published packages.

The settings site is made for a home network only and refuses visitors from elsewhere. Exposing port 8090 to the internet, for example by forwarding it on your router, is outside what it is built for; a way around the home-network check is in scope.
