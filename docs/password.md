# Password and reset codes

- [Codes](#codes)
- [From the command line](#from-the-command-line)
- [If you can't sign in](#if-you-cant-sign-in)

## Codes

The settings have one password per install, at least 10 characters. They refuse one of the 5,000 most common passwords of 10 characters or more, whatever its case (`password123`, `Manchester1`), saying "That's one of the most common passwords. Choose another.", wherever it is set: claiming them, a reset, or the command line. A password set before that rule keeps working. Claiming them the first time, and setting a new password when you've forgotten it, both need a one-time code. The code shows:

- **on the board**, while the board view is on (e-paper can take a minute to change);
- **in the log** otherwise: on a Pi `sudo journalctl -u twobells-web`; in Docker `docker logs twobells`, or in Container Manager, **Container**, `twobells`, **Log**; on a desktop, the Terminal window running it, or, started at login, `~/Library/Logs/twobells.log` on a Mac and `journalctl --user -u twobells` on Linux. The line reads `Settings reset code: 123 456, valid until 14:05`, in the board's time zone (**General**, **Time zone**).

To claim the settings, open them and press **Show a code on the board** (or **Put a code in the log**), then type the code with a new password. A forgotten password works the same way, from **Forgot password?** on the sign-in page; setting a new one signs every browser out.

A code lasts 10 minutes and works once. Asking again while it lasts shows the same code (in the log, a line says the last one still stands) and uses up none of the limits. Five wrong tries from one device shut that device out of the code, so no one else can spoil yours (over IPv6, the devices on one network count as one, as a device can change its IPv6 address at will); asking again from that device says so, and the time the code runs out, after which it can ask for a new one. Ten in all, from any devices, void it. A new code can be made once the last is used or runs out, at most once a minute and 10 times a day.

## From the command line

You can set the password from the command line, even while the server runs. It asks for the new password twice and doesn't show it as you type; every browser signed in is signed out. `--reset-code` prints a new code straight to you instead (no limits, and it replaces any code already out), to type on the sign-in page under **Forgot password?**, or on the claim page the first time.

- **Pi:**

  ```bash
  sudo -u twobells /opt/twobells/venv/bin/twobells serve --install pi --set-password
  sudo -u twobells /opt/twobells/venv/bin/twobells serve --install pi --reset-code
  ```

  Run as root, it refuses, with the line `Run this as the service's user: sudo -u twobells …`: a file root wrote in the store would be root's alone, and the services would fail on it.
- **Docker**, on the NAS over SSH:

  ```bash
  docker exec -it twobells python -m twobells serve --set-password --state-dir /data
  docker exec -it twobells python -m twobells serve --reset-code --state-dir /data
  ```

  Put `sudo` in front if Docker refuses permission. Both write only to the container's `/data` volume.
- **Desktop:** `twobells serve --set-password` or `twobells serve --reset-code`, with the same `--config` and `--state-dir` as you start it with, if any.

## If you can't sign in

- **"That password isn't right":** after 5 wrong tries within 15 minutes, that device is locked out until the first of them is 15 minutes old, and sees "Too many attempts. Try again in 15 minutes." Over IPv6 the devices on one network count as one, as a device can change its IPv6 address at will, so a wrong guess on another device can lock yours out too; a device using IPv4 counts on its own.
- **"Lots of wrong passwords have been tried lately":** after 20 wrong tries in 15 minutes from all devices together, signing in slows for everyone, so a guesser gains nothing by changing address: one try at a time is let in, a little after the last, at most a minute apart, and devices take turns in the order they first tried. Try again in a minute; a device that tries again within two minutes keeps its place in the line. If someone keeps guessing, sign in with a reset code instead (it isn't slowed), from the page's **Forgot password?** or the command line above.
- **"Busy, try again in a moment":** a few sign-ins were being checked at once; try again.
- **"Forbidden":** the settings only answer devices on the home network, using a local name or address. They won't work over mobile data or a VPN to elsewhere.

The settings use plain HTTP on your home network. Use a password you don't use anywhere else, and don't forward port 8090 on your router. See [How the web server is kept safe](security.md).
