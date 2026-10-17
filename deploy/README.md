# CockBook auf einem Server (kostenlos, Oracle Cloud „Always Free“)

Alles läuft auf einer VM: **Caddy** liefert die Seite per HTTPS aus und leitet `/ws` intern
ans **Backend** weiter. Das Backend ist von außen nicht direkt erreichbar und verlangt ein
Zugangs-Token. Die Langzeit-Heatmap sammelt rund um die Uhr und liegt in einem Docker-Volume.

```
Browser ──https──▶ VM:  Caddy ── /    → Frontend
                           └── /ws  → Backend (nur intern) ──▶ Binance
```

Kosten: 0 €, solange du im „Always Free“-Kontingent bleibst.

> **Ehrlich vorab:** Oracle hat das Gratis-Kontingent 2026 schon einmal ohne Ankündigung
> verkleinert, in Frankfurt sind ARM-Server oft „Out of capacity“, und Oracle kann kaum genutzte
> Gratis-Server zurückfordern. Ein Gratis-Server ist bequem, aber keine Garantie. Die Daten der
> Langzeit-Heatmap solltest du darum nicht als unersetzlich betrachten.

---

## 1. Oracle-Konto anlegen

1. Auf [oracle.com/cloud/free](https://www.oracle.com/cloud/free/) registrieren.
2. **Home-Region: Germany Central (Frankfurt)** wählen – lässt sich später **nicht** ändern.
   Nicht USA: Binance sperrt die Futures-API für US-Server.
3. Die Kreditkarte dient nur zur Identitätsprüfung. **Nicht** auf „Pay As You Go“ upgraden,
   dann wird nichts abgebucht.

## 2. VM erstellen

*Compute → Instances → Create instance*

- **Image:** Canonical Ubuntu 24.04
- **Shape** (eine der beiden, beide „Always Free eligible“):
  - **VM.Standard.A1.Flex** (ARM): 2 OCPU, 12 GB RAM – empfohlen. Bei „Out of capacity“ später
    erneut versuchen (oft hilft es, eine andere *Availability Domain* zu wählen).
  - **VM.Standard.E2.1.Micro** (AMD): 1 GB RAM, schwache CPU – meist sofort verfügbar, dann
    CockBook verkleinern (siehe Schritt 7).
- **SSH-Key:** „Generate key pair“ → privaten Schlüssel herunterladen und gut aufheben.
- Nach dem Start die **Public IP** notieren.

Einloggen (Linux/macOS, unter Windows z. B. PowerShell):

```bash
ssh -i pfad/zum/schluessel.key ubuntu@<PUBLIC-IP>
```

## 3. Ports 80 und 443 freigeben (bei Oracle an **zwei** Stellen)

**a) In der Oracle-Konsole:** Instance → *Virtual Cloud Network* → *Security Lists* → Default
Security List → *Add Ingress Rules*:

| Source CIDR | Protokoll | Ziel-Port |
|---|---|---|
| 0.0.0.0/0 | TCP | 80 |
| 0.0.0.0/0 | TCP | 443 |
| 0.0.0.0/0 | UDP | 443 |

**b) In der VM** (Oracles Ubuntu-Images blockieren zusätzlich per iptables):

```bash
sudo iptables -I INPUT 6 -m state --state NEW -p tcp --dport 80 -j ACCEPT
sudo iptables -I INPUT 6 -m state --state NEW -p tcp --dport 443 -j ACCEPT
sudo iptables -I INPUT 6 -p udp --dport 443 -j ACCEPT
sudo netfilter-persistent save
```

## 4. Prüfen, ob Binance vom Server aus erreichbar ist

```bash
curl -s https://fapi.binance.com/fapi/v1/ping
```

`{}` = alles gut. Eine Fehlermeldung mit „restricted location“ (HTTP 451) heißt: Binance sperrt
diesen Standort – dann bringt der Server nichts. (Sperren per VPN o. Ä. zu umgehen, verstößt
gegen die Nutzungsbedingungen von Binance.)

## 5. Kostenlose Adresse (DuckDNS)

Für HTTPS braucht es einen Namen statt nur einer IP.

1. Auf [duckdns.org](https://www.duckdns.org) anmelden (z. B. mit GitHub).
2. Eine Subdomain anlegen, z. B. `meincockbook` → `meincockbook.duckdns.org`.
3. Bei *current ip* die **Public IP** der VM eintragen → *update ip*.

## 6. Docker installieren und CockBook holen

```bash
curl -fsSL https://get.docker.com | sudo sh
sudo usermod -aG docker $USER
exit   # einmal ab- und wieder anmelden, damit die Gruppe gilt
```

```bash
git clone https://github.com/CustomCock/cockbook.git
cd cockbook/deploy
cp .env.example .env
openssl rand -hex 24      # Ausgabe ist dein Token
nano .env                 # COCKBOOK_DOMAIN und COCKBOOK_TOKEN eintragen, speichern
```

## 7. Nur bei der kleinen AMD-VM (1 GB RAM)

Auslagerungsdatei anlegen (gegen Speicherengpässe):

```bash
sudo fallocate -l 2G /swapfile && sudo chmod 600 /swapfile
sudo mkswap /swapfile && sudo swapon /swapfile
echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab
```

In `config.yaml` (im Repo-Ordner) weniger Orderbücher dauerhaft laufen lassen: bei
`ETHUSDT`, `XRPUSDT` und `SOLUSDT` `always_on: true` entfernen – dann sammelt nur BTC
ständig, die anderen starten beim Öffnen. Die ARM-VM braucht das nicht.

## 8. Starten

```bash
docker compose up -d --build
```

Der erste Build dauert einige Minuten. Danach:

```bash
docker compose ps                 # beide Dienste "running"?
docker compose logs -f backend    # Live-Log (Strg+C beendet nur die Anzeige)
```

## 9. Öffnen

Einmal mit Token im Link öffnen – der Teil nach `#` wird nie an einen Server geschickt, der
Browser speichert das Token danach selbst:

```
https://meincockbook.duckdns.org/#token=DEIN-TOKEN
```

Danach reicht `https://meincockbook.duckdns.org`. Auf jedem neuen Gerät/Browser einmal den Link
mit Token öffnen oder das Token in die Abfrage eingeben.

---

## Alltag

| Was | Befehl (im Ordner `cockbook/deploy`) |
|---|---|
| Update auf neueste Version | `git pull && docker compose up -d --build` |
| Neustart | `docker compose restart` |
| Stoppen | `docker compose down` (Daten bleiben erhalten) |
| Speicher/CPU ansehen | `docker stats` |
| Token wechseln | neuen Wert in `.env`, dann `docker compose up -d` |

Die Langzeit-Daten liegen im Docker-Volume `cockbook_cockbook_data` und überstehen Updates.
`docker compose down -v` würde sie **löschen**.

## Sicherheit

- Das Token ist der einzige Schutz für die Daten-Verbindung: lang (≥ 24 Zeichen), zufällig,
  nicht teilen. Es steht nur in `deploy/.env` auf dem Server (nie im Repo).
- Das Backend nimmt nur Verbindungen mit Token **und** von der eigenen Seite an.
- Halte die VM aktuell: `sudo apt update && sudo apt upgrade` (oder `unattended-upgrades`).
- SSH nur mit Schlüssel (Standard bei Oracle), keinen Passwort-Login aktivieren.
