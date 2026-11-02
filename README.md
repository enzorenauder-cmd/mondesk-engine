# Mon Desk – moteur technique

Données de marché pour le Radar Forex de l'app Mon Desk.

- `engine.py` : méthode technique en code (structure, zones, setups S1-S4, entrée/SL/TP, score).
- `fetch_data.py` + `.github/workflows/data.yml` : chaque heure (lun-ven), récupère les bougies Twelve Data des 29 paires et les publie sur la branche `data` (`data.tar.gz` + `meta.json`).

La clé Twelve Data est un secret du dépôt (`TWELVE_DATA_KEY`) et n'apparaît jamais dans les fichiers.
Ce n'est pas un conseil financier.

## Backtest

- `hist_fetch.py` + `.github/workflows/hist.yml` : télécharge l'historique 2023-aujourd'hui (branche `hist`) le soir et la nuit, sans empiéter sur les crédits du Radar.
- `backtest.py` : rejoue le moteur à chaque passage du Radar (bougies clôturées uniquement) et suit chaque signal jusqu'au TP, SL ou expiration.
- `summarize.py` : statistiques par setup, session, paire, RR, score ; réglages choisis sur 2023-06/2025 et vérifiés sur 07/2025-aujourd'hui. Résultats sur la branche `results`.
