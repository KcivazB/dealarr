# dealarr

Suivi de prix ciblé, sans le bruit d'un agrégateur : tu listes les articles qui t'intéressent, dealarr relève
leur prix sur plusieurs boutiques et te notifie **uniquement** quand ça vaut le coup.

## Règles d'alerte

| Règle | Déclenche quand | Disponible |
|---|---|---|
| **Seuil** | meilleur prix ≤ `target_price` × (1 + `tolerance_pct`) | dès le 1er relevé |
| **Plus bas** | prix ≤ plus bas 90 j × (1 + tolérance) **et** nettement sous le prix habituel | après `min_history_days`, ou tout de suite avec Keepa |
| **Vraie baisse** | prix ≤ médiane 30 j × (1 − `drop_pct`) | idem |

- Le **meilleur prix** est calculé toutes boutiques confondues, **frais de port inclus** et ruptures exclues.
  Le port vient, par priorité : `shipping` forcé sur l'offre > seuil de gratuité de la boutique atteint >
  montant lu dans la page (JSON-LD `shippingDetails`, bloc livraison Amazon) > paliers / coût de la boutique
  (section `stores:`) > `default_shipping` (estimé, signalé comme tel). Le port est calculé pour **un seul
  article** : un panier groupé qui atteint le seuil de gratuité n'est pas pris en compte.
- **Anti-spam** : une seule alerte par épisode de promo. Nouvelle alerte seulement si le prix baisse encore de
  `renotify_drop_pct`, ou après remontée du prix puis `cooldown_hours` (réarmement).
- **Prix suspect** (< 35 % du prix habituel) : l'alerte part quand même, avec un avertissement.
- **Suivi cassé** : après `DEALARR_FAILURE_THRESHOLD` échecs consécutifs sur une offre (anti-bot, page modifiée,
  produit retiré), une alerte technique part sur le topic système, puis une autre quand le suivi est rétabli.

## Démarrage

```bash
python -m venv .venv && .venv/Scripts/pip install -e ".[dev]"   # Linux : .venv/bin/pip
cp .env.example .env
dealarr serve                # interface web (http://127.0.0.1:8080) + vérifications planifiées
```

La watchlist vit dans la base SQLite (`data/dealarr.db`). Au premier démarrage, si la base est vide et
qu'un `watchlist.yaml` existe, il est importé automatiquement.

## Interface web

- **Articles** : meilleur prix port compris, boutique, écart au seuil, plus bas, tendance 60 jours, état
  (alerte envoyée, proche du seuil, offres en échec). Bouton « Tout vérifier ».
- **Ajouter** : coller les URLs (une par boutique) → **aperçu** (titre, prix, port, total, stock, déjà suivi ?)
  → nom pré-rempli, seuil, première vérification immédiate.
- **Fiche article** : graphique du meilleur prix par jour (seuil en pointillés, survol, tableau des valeurs),
  détail par boutique (prix, port et son origine, stock, échecs), ajout/modification/retrait de boutiques
  (port forcé, sélecteur CSS, navigateur), règles d'alerte propres à l'article, historique des alertes.
- **Boutiques** : politiques de livraison (coût, offert dès, paliers, note), domaines sans politique, offres en échec.
- **Réglages** : règles par défaut, port estimé, notification de test, export / import YAML.

### Sécurité de l'interface

- **Aucune authentification intégrée** : à exposer uniquement derrière un proxy authentifiant
  (Authentik en forward-auth via Traefik) ou en local. Par défaut, `serve` écoute sur 127.0.0.1.
- CSRF : jeton aléatoire (cookie `SameSite=Strict`, `HttpOnly`) vérifié sur chaque POST + contrôle de l'en-tête `Origin`.
- CSP stricte (aucun script/style inline), `X-Frame-Options: DENY`, `Referrer-Policy: no-referrer`, autoescape des gabarits.
- **Anti-SSRF** : les URLs saisies ne peuvent pas cibler d'adresse non publique (localhost, réseaux privés,
  link-local / métadonnées cloud), redirections comprises. Limite connue : en mode navigateur (Playwright),
  seule l'URL initiale est contrôlée.
- Derrière un proxy, définir `DEALARR_TRUSTED_PROXIES` (IP du proxy, ou `*` si seul Traefik peut joindre le pod)
  pour que le cookie soit marqué `Secure` en HTTPS.

## Ligne de commande

```bash
dealarr serve [--host --port --no-scheduler]   # interface + planificateur
dealarr run                  # planificateur seul (sans interface)
dealarr check --dry-run      # un cycle, notifications en console
dealarr probe <url>          # tester l'extraction d'une page
dealarr status               # derniers prix par boutique
dealarr history <id>         # meilleur prix par jour
dealarr import-yaml [fichier] [--replace]   # YAML → base
dealarr export-yaml [fichier]               # base → YAML (sauvegarde)
dealarr test-notify
```

Docker : `docker compose up -d --build` (interface publiée sur 127.0.0.1:8080 uniquement ; cible `browser`
pour embarquer Playwright). Kubernetes : un Deployment `dealarr serve` (1 réplique, volume persistant pour
`/data`, SQLite ne supportant pas plusieurs écrivains répartis).

## Boutiques

Ajout d'un article : **+ Ajouter** → nom du jeu (+ EAN si tu l'as) → dealarr cherche dans les boutiques
compatibles, regroupe les fiches par code-barres et te laisse cocher celles à suivre. Avec l'EAN, les
boutiques qui n'ont pas remonté le bon produit sont relancées directement avec le code-barres.

| Boutique | Recherche auto | Format d'URL d'une fiche (ajout manuel) |
|---|---|---|
| Philibert | ✅ | `https://www.philibertnet.com/fr/<éditeur>/<id>-<nom>-<ean>.html` |
| Ludum | ✅ | `https://www.ludum.fr/<catégorie>/<nom>-<id>` |
| BCD Jeux | ✅ | `https://www.bcd-jeux.fr/<catégorie>/<id>-<nom>-<ean>.html` |
| Le Passe Temps | ✅ | `https://www.le-passe-temps.com/<catégorie>/<id>-<nom>.html` |
| Le Pion Magique | ✅ | `https://www.lepion.com/jeu/<nom>/` |
| Esprit Jeu | ✅ | `https://www.espritjeu.com/<catégorie>/<nom>.html` |
| IPA Gameshop | ✅ | `https://ipa-gameshop.com/fr/<catégorie>/<id>-<variante>-<nom>.html` (l'id de variante = la langue) |
| La Grande Récré | ✅ | `https://www.lagranderecre.fr/<catégories>/<nom>.html` |
| E.Leclerc | ✅ | `https://www.e.leclerc/fp/<nom>-<ean>` (le prix peut être celui d'un vendeur marketplace) |
| King Jouet | ❌ manuel | `https://www.king-jouet.com/jeu-jouet/<catégories>/ref-<id>-<nom>.htm` |
| Cultura | ❌ manuel | `https://www.cultura.com/p-<nom>-<ean>.html` |
| Amazon | ❌ manuel | `https://www.amazon.fr/dp/<ASIN>` (les paramètres de suivi sont retirés automatiquement) |
| Fnac | ❌ non suivi | protégé par un anti-bot |
| LEGO.com | ❌ non suivi | protégé par Cloudflare : lien direct pour vérifier à la main |

King Jouet, Cultura et Amazon : le suivi des prix fonctionne tant que le site répond, seule leur **recherche**
est inaccessible (JavaScript + anti-robot). La page de recherche propose un lien direct vers chacune ; copier
l'URL de la fiche puis l'ajouter dans la fiche de l'article (« + Ajouter une boutique »).

### LEGO

Type « LEGO » (ou automatique) + **numéro de set** (ex. `75313`) : recherche sur **E.Leclerc** et
**La Grande Récré** ; le set est reconnu par son numéro (et « lego ») dans l'URL ou le titre.
King Jouet, Cultura, Amazon et LEGO.com : liens directs pour vérifier à la main.

### Sites protégés contre les robots

dealarr **ne contourne aucune protection anti-bot** (Cloudflare, DataDome, captchas…) : un site qui bloque
est signalé en échec (alerte technique après plusieurs échecs consécutifs) et reste à suivre manuellement.
Le mode navigateur (image `browser`) ne sert qu'aux pages qui ont besoin de JavaScript pour s'afficher.

## Notifications

Les canaux sont listés dans `DEALARR_NOTIFIERS` (ex. `console,ntfy`). Pour ajouter un canal (Telegram, Discord…) :
une classe avec `name` et `send(Notification)` dans `dealarr/notify/`, puis l'enregistrer dans `build_notifiers`.

**ntfy** : `NTFY_URL`, `NTFY_TOPIC`, `NTFY_TOPIC_SYSTEM` (optionnel), et `NTFY_TOKEN` (recommandé) ou
`NTFY_USER`/`NTFY_PASSWORD`. Publication via l'API JSON (titre, priorité, tags, lien au clic, boutons d'action
vers la boutique, Keepa et CamelCamelCamel pour Amazon).

## Keepa (optionnel)

Avec `KEEPA_API_KEY` (API payante), les produits ayant une offre Amazon récupèrent le plus bas et la moyenne Keepa
sur 90 jours (mis en cache 24 h). Les règles « plus bas » et « vraie baisse » fonctionnent alors sans attendre
l'historique local. ⚠ Intégration non testée avec une vraie clé : valider avec `dealarr probe --keepa <url amazon>`.
Sans clé, les notifications Amazon contiennent quand même des liens vers les historiques Keepa et CamelCamelCamel.

## Sécurité

- Aucun secret dans le code : tout passe par les variables d'environnement (`.env` est ignoré par Git).
- Les logs httpx sont coupés en INFO, pour ne jamais écrire d'URL contenant la clé Keepa.
- Conteneur sans privilèges : utilisateur non-root, système de fichiers en lecture seule, `cap_drop: ALL`.
- Usage personnel : une requête toutes les quelques heures par page, avec un délai entre deux requêtes vers un même site.
