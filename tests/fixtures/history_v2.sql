-- A version 2 punctuality history as History wrote it (twobells at 87d59e4), dumped with sqlite3's
-- iterdump. Outcomes: a train and a bus final, a train still on the board. Arrivals: one reported,
-- one settled from its estimate, one still followed, one cancelled. Times are naive local.
BEGIN TRANSACTION;
CREATE TABLE arrival (
    day TEXT NOT NULL,
    scheduled TEXT NOT NULL,
    route TEXT NOT NULL,
    station TEXT NOT NULL,
    station_name TEXT NOT NULL,
    identity TEXT,
    operator TEXT NOT NULL,
    destination TEXT NOT NULL,
    booked TEXT NOT NULL,
    expected TEXT NOT NULL,
    known INTEGER NOT NULL,
    actual TEXT,
    cancelled INTEGER NOT NULL,
    last_seen TEXT NOT NULL,
    final INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (day, route, scheduled, station)
);
INSERT INTO "arrival" VALUES('2026-10-08','07:42','train|GLO>MAN|Manchester Piccadilly','MAN','Manchester Piccadilly','C1','Northern','Manchester Piccadilly','2026-10-08T08:05:00','2026-10-08T08:20:00',1,'2026-10-08T08:22:00',0,'2026-10-08T08:23:00',1);
INSERT INTO "arrival" VALUES('2026-10-09','22:25','train|GLO>MAN|Manchester Piccadilly','MAN','Manchester Piccadilly','C3','Northern','Manchester Piccadilly','2026-10-09T22:57:00','2026-10-09T23:14:00',1,NULL,0,'2026-10-09T23:12:00',1);
INSERT INTO "arrival" VALUES('2026-10-09','23:55','train|GLO>MAN|Manchester Piccadilly','MAN','Manchester Piccadilly','C2','Northern','Manchester Piccadilly','2026-10-10T00:27:00','2026-10-10T00:31:00',1,NULL,0,'2026-10-09T23:40:00',0);
INSERT INTO "arrival" VALUES('2026-10-09','21:25','train|GLO>MAN|Manchester Piccadilly','MAN','Manchester Piccadilly','C4','Northern','Manchester Piccadilly','2026-10-09T21:57:00','2026-10-09T21:57:00',0,NULL,1,'2026-10-09T21:20:00',1);
CREATE TABLE outcome (
    mode TEXT NOT NULL CHECK (mode IN ('train', 'bus')),
    day TEXT NOT NULL,
    scheduled TEXT NOT NULL,
    route TEXT NOT NULL,
    identity TEXT,
    service TEXT NOT NULL,
    destination TEXT NOT NULL,
    platform TEXT,
    status TEXT NOT NULL,
    delay_min INTEGER,
    live INTEGER NOT NULL,
    reason TEXT,
    first_seen TEXT NOT NULL,
    last_seen TEXT NOT NULL,
    departs_at TEXT NOT NULL,
    final INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (mode, day, route, scheduled)
);
INSERT INTO "outcome" VALUES('train','2026-10-08','07:42','train|GLO>MAN|Manchester Piccadilly','C1','Northern','Manchester Piccadilly','1','late',6,1,'A signal fault','2026-10-08T07:47:00','2026-10-08T07:47:00','2026-10-08T07:48:00',1);
INSERT INTO "outcome" VALUES('bus','2026-10-08','07:20','bus|A|341|Hyde','vj-1','341','Hyde',NULL,'on_time',0,1,NULL,'2026-10-08T07:19:00','2026-10-08T07:19:00','2026-10-08T07:20:00',1);
INSERT INTO "outcome" VALUES('train','2026-10-09','23:55','train|GLO>MAN|Manchester Piccadilly','C2','Northern','Manchester Piccadilly','2','on_time',0,1,NULL,'2026-10-09T23:40:00','2026-10-09T23:40:00','2026-10-09T23:55:00',0);
CREATE INDEX outcome_route ON outcome (mode, route, scheduled, final, day);
CREATE INDEX arrival_open ON arrival (final, expected);
CREATE INDEX arrival_day ON arrival (station, final, day);
COMMIT;
PRAGMA user_version=2;
