"""The setup wizard's internet work, done by a separate service on the Pi.

Why. The settings site's service (twobells-web.service) reaches only
the home network: its kernel IP filter denies everything else, so the page
can't open a connection to the internet itself. That narrows what a flaw
in the page could do; it isn't a wall. Both units run as the same user and
share the board's state folder, and the page still says what the setup
service looks up, within the checks below (Trust). But the wizard's lookups
(postcodes.io, Nominatim, NaPTAN, the OpenStreetMap foot router), its key
tests and its search for bus companies need the internet. On the Pi they
run in the fetch service, twobells-fetch.service (fetch/service.py), instead:
outbound only, started when setup is used, and gone a minute after it was
last asked anything unless the dashboard view is on (it runs the
dashboard's feeds as well). Docker and a desktop have no such filter and
do them in-process.

How the two talk. Through files in a spool folder only the service user
can open (0700):

  tmp/       files being written, by either side
  requests/  the page's requests, one file each: <id>.json
  replies/   the service's answers to lookups: <id>.json
  status/    the search's status: <id>.json
  snapshots/ the dashboard's newest snapshot: dashboard.json (see fetch/handoff.py)

One more request needs no reply: `active`, which the page sends at most
once a minute while a browser uses the dashboard (Poster).

Where. In memory, never written as a file to the SD card: a request can
hold home's exact position or a pasted key, and flash can keep what was
deleted. On the Pi both units name the same systemd RuntimeDirectory,
/run/twobells-fetch (a tmpfs, kept while either runs and until the Pi
restarts), and find it in $RUNTIME_DIRECTORY. Run by hand, they use that
folder if it is there; with neither they refuse, rather than fall back to a
folder on the card. A run on a Mac, or a test, names a folder with
$RUNTIME_DIRECTORY (see spool_folder). Memory itself can still reach the
card if the Pi swaps to it (dphys-swapfile, say), as with anything else.

Every file is written new in tmp/ (0600, never through a link) and renamed
into place whole, so neither side ever reads half of one. A systemd path
unit, twobells-fetch.path, starts the service whenever requests/ isn't
empty: the settings site needs no sudo, polkit rule or access to systemd,
only a file in a folder it already owns, and it can't choose what the
service runs or with what settings. The service takes each request,
deleting it as it reads it (so a pasted key is kept as briefly as it
can), checks it, does it, and writes the reply. The page waits for the
reply, up to a limit per kind, deletes it once read, and deletes its own
request if it is still there when it gives up. A reply that comes after
that is swept away once the longest wait is over, by both sides (the
service looks every time round), and every reply goes when the service
stops, as no page can still be waiting for one then. The search's reply is its status, in
status/, rewritten as it goes and every few seconds besides (from a thread
of its own, so a slow lookup doesn't hold it up), so the page can tell a
stopped service from a slow search. A finished search's status waits longer
to be read: its result took minutes to find. A search is done once its
timetable is saved; its status goes on with the roads for the bus maps
(`roads`) until they are found, and the service stays up for them.

Never a restart loop. systemd starts the service again whenever it stops
with a request still waiting, so it never stops early and leaves one: a
second service, finding the lock taken, stops at once and quietly (the
first answers everything), and one that can't read the board's config
answers every request with NO_SETTINGS until it has been idle a minute.
Nor does the web server wake it for a dashboard it can't run: while the
dashboard's settings don't load, the fetch service stops when idle, and
the web server, checking them the same way, doesn't ask for it until a
save makes them load. The units' start limits catch the rest.

Priority. The search for bus companies downloads and reads timetables for
minutes on a small Pi, so it runs SEARCH_NICE steps nicer than the rest:
os.nice() in the search's own thread, which on Linux lowers that thread
alone (discover.SetupJob's `nice`), and needs no second process. The lookups the wizard
waits on, answered by the service's main thread, keep the usual priority.

Trust. Only the service user can write in the spool, but each side still
treats what the other wrote as untrusted: a file must be a plain file the
service user owns, with one link, under its size cap, named by a request id,
fresh, and strict JSON in exactly the shape expected for its kind. Anything
else is deleted (or, for an awaited reply, refused). Requests carry only
what was typed or pasted: never a saved key (the service reads those as the
other units do) and never settings. Logs name a request's kind, never its
contents: no position, postcode, address or key.
"""
