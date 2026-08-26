#!/usr/bin/env python3
"""Holt Solarprognosen von forecast.solar, speichert sie als TXT und in SQLite.

Nur Standardbibliothek - auf einem Raspberry Pi (Python 3.9+) ohne pip lauffaehig.
Aufruf:  ./solarprognose.py [--config PFAD] [--force] [--dry-run]

Konfiguration und Daten liegen bewusst ausserhalb des Repositorys, damit ein
"git pull" niemals eigene Einstellungen oder Messwerte anfasst:
    Konfiguration  ~/.config/solarprognose/config.ini
    Daten          ~/solarprognose/  (TXT-Dateien, Datenbank, Logs)
Beides ist ueber --config bzw. base_dir frei verschiebbar.
"""

import argparse
import configparser
import logging
import logging.handlers
import os
import shlex
import shutil
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, date, timezone
from zoneinfo import ZoneInfo

API_BASE = "https://api.forecast.solar/estimate"
USER_AGENT = "solarprognose-logger/1.0 (+stdlib)"
VALID_TYPES = ("watts", "watt_hours_period", "watt_hours", "watt_hours_day")

HERE = os.path.dirname(os.path.abspath(__file__))
USER_CONFIG = os.path.join(os.path.expanduser("~"), ".config", "solarprognose", "config.ini")
DEFAULT_BASE_DIR = os.path.join(os.path.expanduser("~"), "solarprognose")

log = logging.getLogger("solarprognose")


# --------------------------------------------------------------------------
# Konfiguration
# --------------------------------------------------------------------------

class Standort:
    def __init__(self, label, sec):
        self.label = label
        self.latitude = sec.get("latitude")
        self.longitude = sec.get("longitude")
        self.declination = sec.get("declination")
        self.azimuth = sec.get("azimuth")
        self.kwp = sec.get("kwp")
        self.tzname = sec.get("timezone", "Europe/Berlin")
        missing = [k for k in ("latitude", "longitude", "declination", "azimuth", "kwp")
                   if not getattr(self, k)]
        if missing:
            raise ValueError("Standort '%s': fehlende Angaben: %s" % (label, ", ".join(missing)))
        try:
            self.tz = ZoneInfo(self.tzname)
        except Exception as exc:
            raise ValueError("Standort '%s': unbekannte Zeitzone '%s' (%s)"
                             % (label, self.tzname, exc))

    @property
    def url(self):
        return "%s/%s/%s/%s/%s/%s" % (API_BASE, self.latitude, self.longitude,
                                      self.declination, self.azimuth, self.kwp)


def find_config(explicit):
    """Konfiguration suchen: --config, dann Umgebungsvariable, dann Standardorte."""
    if explicit:
        if not os.path.isfile(explicit):
            raise SystemExit("Konfigurationsdatei nicht gefunden: %s" % explicit)
        return explicit
    env = os.environ.get("SOLARPROGNOSE_CONFIG")
    if env:
        if not os.path.isfile(env):
            raise SystemExit("SOLARPROGNOSE_CONFIG zeigt auf eine nicht vorhandene Datei: %s" % env)
        return env
    # Repo-lokale config.ini nur als Rueckfallebene, damit Entwicklung auf einem
    # Arbeitsplatzrechner ohne Extra-Pfade funktioniert.
    for candidate in (USER_CONFIG, os.path.join(HERE, "config.ini")):
        if os.path.isfile(candidate):
            return candidate
    raise SystemExit(
        "Keine Konfiguration gefunden. Gesucht wurde in:\n"
        "  %s\n  %s\n\n"
        "Einmalig anlegen mit:\n"
        "  python3 %s --init-config" % (USER_CONFIG, os.path.join(HERE, "config.ini"),
                                        os.path.join(HERE, "solarprognose.py")))


def init_config(target=None):
    """Legt einmalig eine Konfiguration aus der Vorlage an."""
    target = target or USER_CONFIG
    vorlage = os.path.join(HERE, "config.example.ini")
    if not os.path.isfile(vorlage):
        raise SystemExit("Vorlage nicht gefunden: %s" % vorlage)
    if os.path.isfile(target):
        raise SystemExit("Es gibt bereits eine Konfiguration: %s\n"
                         "Sie wird nicht ueberschrieben - bitte direkt bearbeiten." % target)
    os.makedirs(os.path.dirname(target), exist_ok=True)
    shutil.copyfile(vorlage, target)
    try:
        os.chmod(target, 0o600)
    except OSError:
        pass
    print("Konfiguration angelegt: %s\n\n"
          "Jetzt die Standorte eintragen:\n  nano %s\n\n"
          "Diese Datei liegt ausserhalb des Repositorys - ein 'git pull' fasst sie nie an."
          % (target, target))
    return 0


def load_config(path):
    cp = configparser.ConfigParser()
    cp.read(path, encoding="utf-8")
    if not cp.has_section("global"):
        raise SystemExit("Abschnitt [global] fehlt in %s" % path)

    g = cp["global"]
    config_dir = os.path.dirname(os.path.abspath(path))

    # base_dir bestimmt, worauf sich relative Pfade beziehen. Ohne Angabe landen
    # Daten in ~/solarprognose, also ausserhalb des Repositorys. Ein relativ
    # angegebenes base_dir wird gegen das Verzeichnis der Konfiguration aufgeloest.
    base = g.get("base_dir", "").strip()
    if base:
        base = os.path.expanduser(base)
        if not os.path.isabs(base):
            base = os.path.normpath(os.path.join(config_dir, base))
    else:
        base = DEFAULT_BASE_DIR

    def abspath(value):
        value = os.path.expanduser(value)
        return value if os.path.isabs(value) else os.path.normpath(os.path.join(base, value))

    cfg = {
        "config_path": os.path.abspath(path),
        "base_dir": base,
        "output_dir": abspath(g.get("output_dir", "data")),
        "db_path": abspath(g.get("db_path", "solarprognose.db")),
        "log_file": abspath(g.get("log_file", "logs/solarprognose.log")),
        "filename_prefix": g.get("filename_prefix", "Solardaten"),
        "timeout": g.getint("timeout_seconds", 60),
        "retries": max(1, g.getint("retries", 3)),
        "retry_delay": g.getint("retry_delay_seconds", 60),
        "delay_between": g.getint("delay_between_locations", 10),
        "skip_if_done": g.getboolean("skip_if_already_fetched_today", True),
        "notify_command": g.get("notify_command", "").strip(),
    }

    standorte = [Standort(s.split(":", 1)[1].strip(), cp[s])
                 for s in cp.sections() if s.startswith("Standort:")]
    if not standorte:
        raise SystemExit("Keine [Standort:...]-Abschnitte in %s gefunden" % path)
    return cfg, standorte


def setup_logging(log_file, verbose):
    os.makedirs(os.path.dirname(log_file), exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(message)s", "%Y-%m-%d %H:%M:%S")
    fh = logging.handlers.RotatingFileHandler(log_file, maxBytes=1000000,
                                              backupCount=5, encoding="utf-8")
    fh.setFormatter(fmt)
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    log.setLevel(logging.DEBUG if verbose else logging.INFO)
    log.addHandler(fh)
    log.addHandler(sh)


# --------------------------------------------------------------------------
# Datenbank
# --------------------------------------------------------------------------

SCHEMA = """
-- Bewusst kein WAL: im WAL-Modus braucht selbst ein reiner Leser (Grafana)
-- Schreibrechte auf die -shm-Datei. Bei einem Schreibvorgang pro Tag ist
-- der klassische Journal-Modus die einfachere und sicherere Wahl.
PRAGMA journal_mode = DELETE;

CREATE TABLE IF NOT EXISTS abruf (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    standort     TEXT    NOT NULL,
    abruf_utc    TEXT    NOT NULL,
    abruf_epoch  INTEGER NOT NULL,
    erfolg       INTEGER NOT NULL,
    versuche     INTEGER NOT NULL,
    zeilen       INTEGER,
    datei        TEXT,
    fehler       TEXT
);
CREATE INDEX IF NOT EXISTS idx_abruf_standort ON abruf(standort, abruf_epoch);

CREATE TABLE IF NOT EXISTS prognose (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    abruf_id     INTEGER NOT NULL REFERENCES abruf(id) ON DELETE CASCADE,
    standort     TEXT    NOT NULL,
    abruf_epoch  INTEGER NOT NULL,
    typ          TEXT    NOT NULL,
    zeit_lokal   TEXT    NOT NULL,
    zeit_utc     TEXT    NOT NULL,
    zeit_epoch   INTEGER NOT NULL,
    prognosetag  TEXT    NOT NULL,
    wert         REAL    NOT NULL,
    UNIQUE(standort, abruf_epoch, typ, zeit_lokal)
);
CREATE INDEX IF NOT EXISTS idx_prognose_reihe ON prognose(standort, typ, zeit_epoch);
CREATE INDEX IF NOT EXISTS idx_prognose_tag   ON prognose(standort, typ, prognosetag);

-- Nur der jeweils juengste Abruf pro Standort - fuer Grafana meist die richtige Quelle.
CREATE VIEW IF NOT EXISTS prognose_aktuell AS
SELECT p.*
FROM prognose p
JOIN (SELECT standort, MAX(abruf_epoch) AS max_epoch
      FROM prognose GROUP BY standort) m
  ON m.standort = p.standort AND m.max_epoch = p.abruf_epoch;
"""


def open_db(path):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    conn = sqlite3.connect(path, timeout=30)
    conn.executescript(SCHEMA)
    conn.commit()
    return conn


def already_fetched_today(conn, standort, today_iso):
    row = conn.execute(
        "SELECT 1 FROM abruf WHERE standort=? AND erfolg=1"
        " AND date(abruf_utc,'localtime')=? LIMIT 1",
        (standort, today_iso)).fetchone()
    return row is not None


# --------------------------------------------------------------------------
# Abruf
# --------------------------------------------------------------------------

class FetchError(Exception):
    def __init__(self, message, retry_after=None):
        super().__init__(message)
        self.retry_after = retry_after


def fetch_once(url, timeout):
    req = urllib.request.Request(url, headers={
        "Accept": "text/csv",
        "User-Agent": USER_AGENT,
    })
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8")
            remaining = resp.headers.get("X-Ratelimit-Remaining")
            if remaining is not None:
                log.debug("Rate-Limit verbleibend: %s", remaining)
            return body
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode("utf-8", "replace")[:300].replace("\n", " ")
        except Exception:
            pass
        retry_after = None
        if exc.code == 429:
            try:
                retry_after = int(exc.headers.get("Retry-After", "0")) or None
            except (TypeError, ValueError):
                retry_after = None
        raise FetchError("HTTP %s %s %s" % (exc.code, exc.reason, detail), retry_after)
    except urllib.error.URLError as exc:
        raise FetchError("Netzwerkfehler: %s" % exc.reason)
    except TimeoutError:
        raise FetchError("Zeitueberschreitung nach %ss" % timeout)


def validate(body):
    """Stellt sicher, dass wir wirklich das CSV und keine Fehlermeldung haben."""
    if not body or not body.strip():
        raise FetchError("Leere Antwort")
    lines = [l for l in body.splitlines() if l.strip()]
    if not any(l.startswith("watt_hours_day;") for l in lines):
        raise FetchError("Antwort enthaelt keine watt_hours_day-Zeile "
                         "(vermutlich Fehlermeldung): %s" % lines[0][:200])
    if len(lines) < 8:
        raise FetchError("Antwort verdaechtig kurz (%d Zeilen)" % len(lines))
    return lines


def fetch_with_retries(standort, cfg):
    last = None
    for attempt in range(1, cfg["retries"] + 1):
        try:
            log.info("[%s] Abruf Versuch %d/%d: %s",
                     standort.label, attempt, cfg["retries"], standort.url)
            body = fetch_once(standort.url, cfg["timeout"])
            lines = validate(body)
            log.info("[%s] OK - %d Zeilen empfangen", standort.label, len(lines))
            return body, lines, attempt
        except FetchError as exc:
            last = exc
            log.warning("[%s] Versuch %d fehlgeschlagen: %s", standort.label, attempt, exc)
            if attempt < cfg["retries"]:
                wait = exc.retry_after or cfg["retry_delay"] * attempt
                log.info("[%s] warte %ds vor naechstem Versuch", standort.label, wait)
                time.sleep(wait)
    raise last


# --------------------------------------------------------------------------
# Parsen und Speichern
# --------------------------------------------------------------------------

def parse_lines(lines, standort):
    """CSV-Zeilen -> Tupel fuer die DB. Zeiten werden nach UTC konvertiert."""
    rows, skipped = [], 0
    for line in lines:
        parts = line.split(";")
        if len(parts) != 3:
            skipped += 1
            continue
        typ, zeit_raw, wert_raw = (p.strip().strip('"') for p in parts)
        if typ not in VALID_TYPES:
            skipped += 1
            continue
        try:
            wert = float(wert_raw)
        except ValueError:
            skipped += 1
            continue
        try:
            if len(zeit_raw) == 10:                       # watt_hours_day: nur Datum
                naive = datetime.strptime(zeit_raw, "%Y-%m-%d")
            else:
                naive = datetime.strptime(zeit_raw, "%Y-%m-%d %H:%M:%S")
        except ValueError:
            skipped += 1
            continue
        # fold=0: bei der doppelten Stunde der Zeitumstellung die erste waehlen
        lokal = naive.replace(tzinfo=standort.tz, fold=0)
        utc = lokal.astimezone(timezone.utc)
        rows.append((typ, zeit_raw, utc.strftime("%Y-%m-%d %H:%M:%S"),
                     int(utc.timestamp()), naive.date().isoformat(), wert))
    if skipped:
        log.warning("[%s] %d Zeile(n) nicht interpretierbar, uebersprungen",
                    standort.label, skipped)
    return rows


def write_txt(cfg, standort, body, run_date):
    """Schreibt das Originalformat atomar nach <prefix>_<label>_<datum>.txt."""
    os.makedirs(cfg["output_dir"], exist_ok=True)
    name = "%s_%s_%s.txt" % (cfg["filename_prefix"], standort.label, run_date.isoformat())
    target = os.path.join(cfg["output_dir"], name)
    tmp = target + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="") as fh:
        fh.write(body)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, target)
    log.info("[%s] Datei geschrieben: %s", standort.label, target)
    return target


def store(conn, standort, rows, datei, versuche, abruf_dt):
    cur = conn.execute(
        "INSERT INTO abruf (standort, abruf_utc, abruf_epoch, erfolg, versuche,"
        " zeilen, datei, fehler) VALUES (?,?,?,1,?,?,?,NULL)",
        (standort.label, abruf_dt.strftime("%Y-%m-%d %H:%M:%S"), int(abruf_dt.timestamp()),
         versuche, len(rows), datei))
    abruf_id = cur.lastrowid
    epoch = int(abruf_dt.timestamp())
    conn.executemany(
        "INSERT OR IGNORE INTO prognose"
        " (abruf_id, standort, abruf_epoch, typ, zeit_lokal, zeit_utc, zeit_epoch,"
        "  prognosetag, wert) VALUES (?,?,?,?,?,?,?,?,?)",
        [(abruf_id, standort.label, epoch) + r for r in rows])
    conn.commit()
    log.info("[%s] %d Datensaetze in die Datenbank geschrieben", standort.label, len(rows))


def store_failure(conn, standort, fehler, versuche, abruf_dt):
    conn.execute(
        "INSERT INTO abruf (standort, abruf_utc, abruf_epoch, erfolg, versuche,"
        " zeilen, datei, fehler) VALUES (?,?,?,0,?,NULL,NULL,?)",
        (standort.label, abruf_dt.strftime("%Y-%m-%d %H:%M:%S"), int(abruf_dt.timestamp()),
         versuche, str(fehler)))
    conn.commit()


def notify(cfg, standort, fehler):
    cmd = cfg["notify_command"]
    if not cmd:
        return
    cmd = cmd.replace("{standort}", standort.label).replace("{fehler}", str(fehler))
    try:
        subprocess.run(shlex.split(cmd), timeout=30, check=False)
        log.info("[%s] Benachrichtigung abgesetzt", standort.label)
    except Exception as exc:
        log.error("[%s] Benachrichtigung fehlgeschlagen: %s", standort.label, exc)


# --------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(
        description="Solarprognosen abrufen und speichern",
        epilog="Ohne --config wird gesucht in: $SOLARPROGNOSE_CONFIG, %s, %s"
               % (USER_CONFIG, os.path.join(HERE, "config.ini")))
    ap.add_argument("--config", default=None, help="Pfad zur Konfigurationsdatei")
    ap.add_argument("--init-config", action="store_true",
                    help="einmalig eine Konfiguration aus der Vorlage anlegen und beenden")
    ap.add_argument("--force", action="store_true",
                    help="auch abrufen, wenn heute bereits erfolgreich geladen wurde")
    ap.add_argument("--dry-run", action="store_true",
                    help="abrufen und pruefen, aber nichts schreiben")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    if args.init_config:
        return init_config(args.config)

    cfg, standorte = load_config(find_config(args.config))
    setup_logging(cfg["log_file"], args.verbose)

    conn = open_db(cfg["db_path"])
    today = date.today()
    log.info("=== Lauf gestartet (%d Standort(e), %s) ===", len(standorte), today.isoformat())
    log.debug("Konfiguration: %s | Datenverzeichnis: %s", cfg["config_path"], cfg["base_dir"])

    fehlgeschlagen, aktiv = [], []
    for s in standorte:
        if cfg["skip_if_done"] and not args.force and not args.dry_run \
                and already_fetched_today(conn, s.label, today.isoformat()):
            log.info("[%s] heute bereits erfolgreich abgerufen - uebersprungen", s.label)
            continue
        aktiv.append(s)

    for i, s in enumerate(aktiv):
        if i > 0 and cfg["delay_between"] > 0:
            log.debug("Pause %ds vor dem naechsten Standort", cfg["delay_between"])
            time.sleep(cfg["delay_between"])
        abruf_dt = datetime.now(timezone.utc)
        try:
            body, lines, versuche = fetch_with_retries(s, cfg)
            rows = parse_lines(lines, s)
            if not rows:
                raise FetchError("keine verwertbaren Datenzeilen")
            if args.dry_run:
                log.info("[%s] DRY-RUN: %d Datensaetze, %s ... %s",
                         s.label, len(rows), rows[0][1], rows[-1][1])
                continue
            datei = write_txt(cfg, s, body, today)
            store(conn, s, rows, datei, versuche, abruf_dt)
        except Exception as exc:
            log.error("[%s] ENDGUELTIG FEHLGESCHLAGEN: %s", s.label, exc)
            if not args.dry_run:
                store_failure(conn, s, exc, cfg["retries"], abruf_dt)
                notify(cfg, s, exc)
            fehlgeschlagen.append(s.label)

    conn.close()
    if fehlgeschlagen:
        log.error("=== Lauf beendet mit Fehlern: %s ===", ", ".join(fehlgeschlagen))
        return 1
    log.info("=== Lauf erfolgreich beendet ===")
    return 0


if __name__ == "__main__":
    sys.exit(main())
