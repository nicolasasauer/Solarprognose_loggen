# Solarprognose-Logger

Ruft regelmäßig die PV-Ertragsprognose von [forecast.solar](https://forecast.solar) für
mehrere Standorte ab und speichert sie doppelt:

1. **als TXT-Datei** im unveränderten Originalformat der API
   (`Solardaten_<Label>_<Datum>.txt`)
2. **in einer SQLite-Datenbank**, normalisiert und mit UTC-Zeitstempeln, damit
   Grafana daraus Zeitreihen zeichnen kann

Läuft ausschließlich mit der Python-Standardbibliothek — auf einem Raspberry Pi
sind keine `pip`-Pakete nötig.

---

## Wo was liegt

Alles liegt in einem Verzeichnis, im Beispiel `/home/nicolas/scripts/Solarprognose_loggen`:

```
Solarprognose_loggen/
  solarprognose.py            aus Git, wird von git pull aktualisiert
  config.example.ini          aus Git, Vorlage
  config.ini                  deine Einstellungen, in .gitignore
  data/                       die TXT-Dateien
  logs/solarprognose.log      Protokoll
  solarprognose.db            SQLite-Datenbank fuer Grafana
```

`config.ini`, `data/`, `logs/` und `*.db` stehen in `.gitignore`. Ein `git pull`
aktualisiert daher nur den Code und fasst deine Einstellungen und Messwerte nicht
an. Verschieben lässt sich beides trotzdem: die Konfiguration über `--config` oder
`$SOLARPROGNOSE_CONFIG`, das Datenverzeichnis über `base_dir` in der Konfiguration.

> **Achtung:** Da die Daten im Git-Arbeitsverzeichnis liegen, würde ein
> `git clean -xdf` sie mitlöschen — dieser Befehl entfernt auch ignorierte
> Dateien. `git pull`, `git checkout` und `git reset --hard` sind dagegen
> unbedenklich.

---

## Installation auf dem Raspberry Pi

Voraussetzung ist Python 3.9 oder neuer — Raspberry Pi OS Bookworm bringt 3.11
bereits mit, `python3 --version` bestätigt das.

Repository klonen:

```bash
mkdir -p ~/scripts && cd ~/scripts && git clone https://github.com/nicolasasauer/Solarprognose_loggen.git
```

Konfiguration einmalig anlegen:

```bash
python3 /home/nicolas/scripts/Solarprognose_loggen/solarprognose.py --init-config
```

Standorte eintragen:

```bash
nano /home/nicolas/scripts/Solarprognose_loggen/config.ini
```

Testlauf, der abruft und prüft, aber nichts schreibt:

```bash
python3 /home/nicolas/scripts/Solarprognose_loggen/solarprognose.py --dry-run -v
```

Ab jetzt genügt für Code-Updates:

```bash
cd /home/nicolas/scripts/Solarprognose_loggen && git pull
```

---

## Konfiguration

Alles steckt in `config.ini` neben dem Skript. Pro Anlage ein Abschnitt
`[Standort:LABEL]`; das Label landet unverändert im Dateinamen und in der Datenbank.

```ini
[Standort:RT_Tr]
latitude    = 48.137
longitude   = 11.575
declination = 35          ; Neigung in Grad, 0 = waagerecht, 90 = senkrecht
azimuth     = 0           ; -90 = Ost, 0 = Süd, 90 = West, ±180 = Nord
kwp         = 5.5         ; installierte Leistung
timezone    = Europe/Berlin
```

Zwei Nachkommastellen bei den Koordinaten reichen völlig aus — auf ~1 km gerundet
liefert die API ein identisches Ergebnis, das Wettermodell arbeitet ohnehin gröber.

Der Dateiname entsteht aus `filename_prefix`, Label und **Abrufdatum**:
`Solardaten_RT_Tr_2026-08-27.txt`.

Wird mehrmals täglich abgerufen, steuert `txt_mode`, was mit dieser Datei
passiert. Die Datenbank bekommt in jedem Fall **alle** Abrufe:

| Wert | Verhalten |
|---|---|
| `first_of_day` | eine Datei je Tag, nur der erste erfolgreiche Lauf schreibt sie — spätere Läufe gehen nur in die Datenbank |
| `every_run` | eine Datei je Tag, jeder Lauf überschreibt sie mit der neuesten Prognose |
| `timestamped` | eine Datei je Lauf, `Solardaten_RT_Tr_2026-08-27_0500.txt` |

Wichtige Schalter im Abschnitt `[global]`:

| Schlüssel | Bedeutung |
|---|---|
| `base_dir` | Basis für alle relativen Pfade, ohne Angabe das Skriptverzeichnis |
| `timeout_seconds` | Zeitlimit pro HTTP-Versuch (Standard 60) |
| `retries` | Versuche pro Standort und Lauf (Standard 3) |
| `retry_delay_seconds` | Wartezeit zwischen Versuchen, wächst mit jedem Versuch |
| `delay_between_locations` | Pause zwischen zwei Standorten, schont das Rate-Limit |
| `min_interval_minutes` | Mindestabstand zwischen zwei Abrufen desselben Standorts; `170` = dreistündlich, `1440` = täglich, `0` = nie überspringen |
| `txt_mode` | `first_of_day`, `every_run` oder `timestamped` — siehe unten |
| `prune_prognose_after_days` | löscht Prognosezeilen älterer Abrufe, `0` = nie |
| `notify_command` | optionaler Befehl bei endgültigem Fehlschlag, `{standort}` und `{fehler}` werden ersetzt |

---

## Zeitsteuerung per Cron

`crontab -e` öffnen und eintragen:

```cron
0 */3 * * * /usr/bin/python3 /home/nicolas/scripts/Solarprognose_loggen/solarprognose.py >> /home/nicolas/scripts/Solarprognose_loggen/cron.log 2>&1
```

Cron kennt keine Tilde, deshalb der ausgeschriebene Pfad. Die Umleitung nach
`cron.log` fängt Fehler ab, die auftreten, *bevor* das Skript startet — etwa ein
falscher Pfad oder ein fehlendes `python3`. In solchen Fällen gäbe es kein
Skript-Log, in dem man nachsehen könnte.

**Wie oft wirklich geladen wird, entscheidet nicht der Cron-Eintrag, sondern
`min_interval_minutes` in der Konfiguration.** Das Skript überspringt einen
Standort, solange sein letzter Erfolg jünger ist als dieser Abstand. Bei `170`
und einem Cron alle drei Stunden lädt es also achtmal täglich — und wenn ein Lauf
scheitert, holt der nächste ihn nach, ohne dass du eine zweite Cron-Zeile
brauchst. Der Fallback ist damit automatisch eingebaut.

Für den alten Betrieb einmal täglich setzt du `min_interval_minutes = 1440` und
lässt den Cron-Eintrag unverändert dreistündlich feuern; der erste erfolgreiche
Lauf des Tages gewinnt, alle weiteren beenden sich sofort. Der ältere Schalter
`skip_if_already_fetched_today = true` bewirkt dasselbe und wird weiterhin
verstanden.

Innerhalb eines einzelnen Laufs wird ohnehin schon dreimal mit wachsendem Abstand
versucht (0 s / 60 s / 120 s), sodass kurze Störungen gar nicht erst auffallen.

Ein einzelner Standort lässt sich jederzeit von Hand nachziehen:

```bash
python3 /home/nicolas/scripts/Solarprognose_loggen/solarprognose.py --force
```

---

## Datenbank

Zwei Tabellen und eine View:

**`abruf`** — ein Datensatz pro Standort und Lauf, auch bei Misserfolg. Enthält
Zeitpunkt, Erfolgskennzeichen, Anzahl der Versuche, geschriebene Datei und die
Fehlermeldung. Das ist dein Betriebsprotokoll.

**`prognose`** — die eigentlichen Werte:

| Spalte | Inhalt |
|---|---|
| `standort` | Label aus der Konfiguration |
| `abruf_epoch` | wann diese Prognose geholt wurde |
| `typ` | `watts`, `watt_hours_period`, `watt_hours` oder `watt_hours_day` |
| `zeit_lokal` | Zeitstempel wie von der API geliefert (Ortszeit) |
| `zeit_utc` / `zeit_epoch` | derselbe Zeitpunkt in UTC |
| `prognosetag` | Datum, für das die Prognose gilt |
| `wert` | Zahlenwert |

Jeder Abruf wird behalten. Dadurch lässt sich später nachvollziehen, wie sich die
Prognose für einen bestimmten Tag über die Zeit verändert hat, und Prognose gegen
tatsächlichen Ertrag vergleichen.

**`prognose_aktuell`** — dieselbe Struktur, aber gefiltert auf den jeweils
jüngsten Abruf pro Standort. Für die meisten Grafana-Panels die richtige Quelle.

**`prognose_spanne`** — fasst alle Abrufe je Zeitpunkt zusammen und liefert
`anzahl`, `wert_min`, `wert_mittel`, `wert_max` und `spanne`. Damit siehst du,
wie stark eine einzelne Stunde im Lauf des Tages nach oben oder unten korrigiert
wurde. Sinnvoll nur, wenn mehrmals täglich abgerufen wird.

Die vier Typen bedeuten: `watts` ist die Momentanleistung, `watt_hours_period` der
Ertrag innerhalb der Stunde, `watt_hours` der über den Tag aufsummierte Ertrag und
`watt_hours_day` die Tagessumme.

Die kostenlose API liefert immer **heute und morgen** — mehr nicht. Ein
Zeitpunkt für einen bestimmten Tag wird daher von rund sechzehn Abrufen
vorhergesagt: acht am Vortag und acht am Tag selbst.

Bei fünf Standorten und acht Läufen täglich wächst die Datenbank um grob 150 bis
200 MB im Jahr. Auf einer SD-Karte ist das irgendwann spürbar; `prune_prognose_after_days`
begrenzt die Aufbewahrung, wenn du das nicht willst.

---

## Grafana

Die SQLite-Datenquelle einmalig installieren und Grafana neu starten:

```bash
sudo grafana-cli plugins install frser-sqlite-datasource && sudo systemctl restart grafana-server
```

Anschließend eine Datenquelle vom Typ *SQLite* mit dem Pfad zur `solarprognose.db`
anlegen — standardmäßig `/home/nicolas/scripts/Solarprognose_loggen/solarprognose.db`. Der Grafana-Benutzer
braucht Leserechte auf die Datei und auf das darüberliegende Verzeichnis:

```bash
sudo chmod o+rx /home/nicolas/scripts/Solarprognose_loggen && sudo chmod o+r /home/nicolas/scripts/Solarprognose_loggen/solarprognose.db
```

Die Datenbank läuft bewusst nicht im WAL-Modus, denn dort bräuchte selbst ein
reiner Leser Schreibrechte auf die begleitende `-shm`-Datei. Bei einem
Schreibvorgang pro Tag ist der klassische Journal-Modus die einfachere Wahl.

**Leistungsverlauf, aktuellste Prognose:**

```sql
SELECT zeit_epoch AS time, wert AS watt, standort AS metric
FROM prognose_aktuell
WHERE typ = 'watts'
ORDER BY zeit_epoch
```

**Tagessummen aller Standorte:**

```sql
SELECT zeit_epoch AS time, wert / 1000.0 AS kwh, standort AS metric
FROM prognose_aktuell
WHERE typ = 'watt_hours_day'
ORDER BY zeit_epoch
```

**Wie sich die Prognose für morgen entwickelt hat** — zeigt pro Abruf, welche
Tagessumme damals vorhergesagt wurde:

```sql
SELECT abruf_epoch AS time, wert / 1000.0 AS kwh, standort AS metric
FROM prognose
WHERE typ = 'watt_hours_day'
  AND prognosetag = date('now', '+1 day')
ORDER BY abruf_epoch
```

Setze im Panel die Einheit auf *Watt* beziehungsweise *Kilowatt-Stunde* und
deaktiviere bei Bedarf den Zeitfilter des Dashboards, da Prognosen in der Zukunft
liegen.

### Streuung: wie sicher ist die Prognose?

Weil mehrmals täglich abgerufen wird, gibt es je Stunde mehrere Vorhersagen. Aus
ihnen lassen sich Minimum, Mittelwert und Maximum bilden — je weiter die drei
auseinanderliegen, desto unsicherer war sich das Modell:

```sql
SELECT zeit_epoch AS time, wert_min AS min, wert_mittel AS mittel, wert_max AS max
FROM prognose_spanne
WHERE typ = 'watts' AND standort = 'RT_Tr' AND prognosetag = date('now')
ORDER BY zeit_epoch
```

Falls du die View nicht verwenden willst, tut es dieselbe Abfrage direkt im
Panel — die View ist reine Bequemlichkeit:

```sql
SELECT zeit_epoch AS time, MIN(wert) AS min, AVG(wert) AS mittel, MAX(wert) AS max
FROM prognose
WHERE typ = 'watts' AND standort = 'RT_Tr' AND prognosetag = date('now')
GROUP BY zeit_epoch
ORDER BY zeit_epoch
```

Im Panel stellst du *mittel* als durchgezogene Linie dar und lässt *min* und *max*
über **Fill below to** aufeinander verweisen — das ergibt ein Unsicherheitsband
um die mittlere Prognose.

### Verlauf einer einzelnen Stunde

Wie hat sich die Vorhersage für eine bestimmte Stunde entwickelt? Auf der
Zeitachse steht hier der Abrufzeitpunkt, nicht der Prognosezeitpunkt:

```sql
SELECT abruf_epoch AS time, wert AS watt
FROM prognose
WHERE typ = 'watts' AND standort = 'RT_Tr'
  AND zeit_lokal = date('now') || ' 12:00:00'
ORDER BY abruf_epoch
```

### Wie oft ändern sich die Werte überhaupt?

Das Modell hinter forecast.solar aktualisiert deutlich seltener als stündlich.
Nach ein paar Tagen Betrieb zeigt dir diese Abfrage, wie viele der Abrufe
tatsächlich neue Werte gebracht haben — damit kannst du `min_interval_minutes`
begründet nachjustieren:

```sql
SELECT prognosetag,
       COUNT(*)                       AS zeitpunkte,
       SUM(CASE WHEN spanne > 0 THEN 1 ELSE 0 END) AS davon_korrigiert,
       ROUND(AVG(anzahl), 1)          AS abrufe_je_zeitpunkt
FROM prognose_spanne
WHERE typ = 'watts' AND standort = 'RT_Tr'
GROUP BY prognosetag
ORDER BY prognosetag DESC
```

---

## Betrieb

### Hat der Lauf geklappt?

Der schnellste Weg ist der Statusbericht:

```bash
python3 /home/nicolas/scripts/Solarprognose_loggen/solarprognose.py --status
```

```
Konfiguration : /home/nicolas/scripts/Solarprognose_loggen/config.ini
Datenbasis    : /home/nicolas/scripts/Solarprognose_loggen
Datenbank     : .../solarprognose.db (48.0 KB, 92 Datensaetze)

STANDORT         LETZTER ERFOLG      ALTER          ZEILEN  LETZTER FEHLER
--------------------------------------------------------------------------
RT_Tr            2026-08-27 05:00    41 Min             92  -
Garage_Ost       nie                 !                   -  -
Carport_West     2026-08-24 05:00    ! 3 Tage           92  27.08. 05:00  Netzwerkfehler

Juengste Prognose: RT_Tr am 2026-08-27 -> 1.8 kWh

1 von 3 Standort(en) heute geladen.
Mit ! markierte Standorte sind nicht auf dem heutigen Stand.
```

Der Befehl liest nur und legt weder Datenbank noch Logzeile an. Sein Exit-Code ist
`0`, wenn **alle** Standorte heute geladen wurden, sonst `1` — damit lässt er sich
direkt in eine Überwachung hängen. Ein Standort, der in der Datenbank steht, aber
nicht mehr in der Konfiguration, wird eigens gekennzeichnet; das passiert, wenn du
ein Label umbenennst. Die Spalte *Letzter Fehler* bleibt leer, solange der letzte
Fehlversuch älter ist als der letzte Erfolg.

### Log und Rohdaten

Das Skript protokolliert nach `/home/nicolas/scripts/Solarprognose_loggen/logs/solarprognose.log` (rotierend, fünf Dateien à
1 MB). Der Exit-Code ist `0` bei Erfolg und `1`, sobald mindestens ein Standort
endgültig gescheitert ist.

Fehlgeschlagene Läufe der letzten Woche anzeigen:

```bash
sqlite3 /home/nicolas/scripts/Solarprognose_loggen/solarprognose.db "SELECT abruf_utc, standort, fehler FROM abruf WHERE erfolg=0 AND abruf_utc > datetime('now','-7 days');"
```

Für eine aktive Benachrichtigung lässt sich `notify_command` in der Konfiguration
setzen, zum Beispiel über [ntfy.sh](https://ntfy.sh):

```ini
notify_command = curl -s -d "Solarprognose {standort}: {fehler}" https://ntfy.sh/mein-privates-topic
```

### Grenzen und Fallstricke

- Das Rate-Limit der kostenlosen API liegt bei **12 Anfragen pro Stunde und
  IP-Adresse**. Fünf Standorte einmal täglich sind unkritisch, auch mit Retries.
  Beim Testen kommt man dem Limit aber schnell nahe.
- Eine Fehlerantwort der API ist JSON, kein CSV. Das Skript prüft deshalb vor dem
  Schreiben, ob eine `watt_hours_day`-Zeile enthalten ist — eine gute Datei wird
  niemals durch eine Fehlermeldung überschrieben.
- Die TXT-Datei wird erst nach `.tmp` geschrieben und dann umbenannt. Ein Abbruch
  mitten im Schreiben kann also keine halbe Datei hinterlassen.
- Bei der Zeitumstellung im Oktober tritt eine Stunde doppelt auf. Das Skript
  wählt dann konsequent die erste Ausprägung.
