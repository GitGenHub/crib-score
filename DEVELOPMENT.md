# Crib Score — Documentation développeur

Application de pointage pour le crib (variante 9 cartes), à deux joueurs (Dan / Gen).
Fichier unique HTML/CSS/JS, sans dépendance de build, hébergé sur GitHub Pages.

**Démo en ligne :** https://gitgenhub.github.io/crib-score/

---

## 1. Stack technique

- **Aucun framework, aucun build step** — un seul fichier `index.html` contenant HTML, CSS et JS inline.
- **Police externe** : Google Fonts (Playfair Display + Inter), chargée via `@import`.
- **Stockage** : `localStorage` du navigateur (voir section 3).
- **Hébergement** : GitHub Pages, déployé depuis la branche `main`, racine du dépôt.

Aucune dépendance npm, aucun serveur requis pour faire tourner l'app telle quelle.

---

## 2. Structure du fichier

Tout vit dans `index.html` :

```
<head>
  <meta viewport ...>       ← viewport-fit=cover requis pour env(safe-area-inset-*)
  <style>                   ← tout le CSS, variables couleur en :root
</head>
<body>
  <header>                  ← position: sticky, titre + bouton Stats
  <div class="sticky-nav">  ← onglets (actuellement 1 seul visible : Partie/Stats togglés par bouton header)
  <div class="main">
    <div id="tab-score">    ← écran de jeu
    <div id="tab-stats">    ← écran de statistiques
  </div>

  <!-- Overlays (position: fixed) -->
  <div id="numpad-overlay">       ← clavier numérique slide-up
  <div id="win-modal">            ← fin de partie
  <div id="newgame-modal">        ← confirmation nouvelle partie
  <div id="reset-modal">          ← (legacy, reset complet — remplacé par Clear dans Stats)
  <div id="edit-game-modal">      ← modifier/supprimer une partie enregistrée
  <div id="combos-modal">         ← easter egg : top 10 mains théoriques (tap sur le titre)

  <script>                  ← toute la logique JS, vanilla, pas de framework
</body>
```

---

## 3. Modèle de données (localStorage)

Deux clés dans `localStorage` :

### `crib-state-v2` — état de la partie en cours

```js
{
  scores: { dan: 0, gen: 0 },
  dealer: 'dan',              // qui a le crib CE tour-ci
  initialDealer: 'dan',       // qui avait le crib au DÉBUT de la partie (figé pour les stats)
  phase: 'Jeu',                // 'Jeu' | 'Main' | 'Crib' — affiché seulement en Mode Full
  history: [],                 // liste des coups joués, pour undo()
  gameActive: true,
  danName: 'Dan',
  genName: 'Gen',
  highScore: { dan: 0, gen: 0 },   // meilleur coup en un tour, capté auto en Mode Full (>50 seulement)
  nibsCount: { dan: 0, gen: 0 },   // nombre de nibs consignés CETTE partie (avant commit dans stats)
  gameMode: 'board'            // 'board' (Mode Light) | 'turn' (Mode Full)
}
```

Chaque entrée de `history` :
```js
{ player: 'dan', pts: 12, phase: 'Main', before: 34, after: 46, nibsOnly: false }
```
`nibsOnly: true` marque un nib consigné en Mode Light (ne change pas le score, sert juste à compter).

### `crib-stats-v2` — statistiques cumulées + historique des parties

```js
{
  dan: {
    wins, losses, skunksGiven, skunksReceived,
    streak, bestStreak,      // séries de victoires
    everBest,                 // record personnel toutes parties confondues (>50)
    nibs                      // total de nibs, RECALCULÉ depuis stats.games (jamais incrémenté directement)
  },
  gen: { ... même structure },
  games: [
    {
      winner: 'dan', loser: 'gen',
      winScore: 121, loseScore: 68,
      skunk: true,                    // loseScore <= 75
      initialDealer: 'dan',
      date: '2026-01-15T20:03:00.000Z',
      highScoreDan: 67, highScoreGen: null,   // un seul des deux non-null, > 50 sinon null
      nibsDan: 2, nibsGen: 0          // nibs de CETTE partie, source de vérité pour les totaux
    },
    ...
  ]
}
```

### ⚠️ Principe important : les totaux sont dérivés, jamais accumulés à la volée

`stats.dan.nibs`, `stats.dan.everBest`, `stats.dan.streak`/`bestStreak` sont **recalculés
depuis `stats.games`** via `recalcNibs()`, `recalcEverBest()`, `recalcStreaks()` — jamais
incrémentés directement sauf au moment précis où une partie se termine (`endGame()`).

**Pourquoi :** un ancien bug incrémentait les nibs directement sur le total global sans les
attacher à la partie. Supprimer une partie ne retirait donc rien. La correction stocke le
détail par partie (`nibsDan`/`nibsGen` sur chaque `game`) et recalcule le total à chaque
chargement (`init()`) et à chaque modification (`deleteGame()`, `saveEditGame()`).

**Si tu ajoutes une nouvelle stat cumulée**, suis ce patron : stocke le détail sur l'objet
`game`, écris une fonction `recalcXxx()` qui boucle sur `stats.games`, et appelle-la après
tout push/edit/delete — jamais d'incrémentation directe sur `stats.dan.xxx`.

---

## 4. Fonctionnement des deux modes de jeu

| | **Mode Light** (`gameMode: 'board'`) | **Mode Full** (`gameMode: 'turn'`) |
|---|---|---|
| Usage prévu | Score final entré à la fin (jeu sur planche physique) | Score entré à chaque main pendant la partie |
| Boutons Jeu/Main/Crib | Masqués | Visibles |
| Historique de partie | Masqué | Visible |
| Cartes de score | Agrandies (`.light-size`) | Taille normale |
| Nibs (tap sur l'icône) | Incrémente un compteur affiché (`×N`), **ne touche pas le score** | Ajoute +2 au score ET incrémente le compteur |
| High score | Saisi manuellement dans la modale de fin de partie | Capté automatiquement (plus haut coup > 50) |
| Bouton 🏆 (victoire auto) | Applique directement 121 au joueur sélectionné | Idem, disponible aussi |

Le switch se fait via `setGameMode(mode)`, qui toggle les classes CSS et l'affichage des
sections concernées.

---

## 5. Fonctions clés (JS)

| Fonction | Rôle |
|---|---|
| `init()` | Charge localStorage, recalcule les totaux dérivés, démarre le rendu |
| `render()` | Rafraîchit tout l'affichage à partir de `state` + `stats` |
| `addScore(player, pts)` | Ajoute des points, plafonne à 121, déclenche `endGame()` si atteint |
| `addNibs(player)` | Branch selon `gameMode` (voir section 4) |
| `applyVictory(player)` | Bouton 🏆 — complète le score à 121 d'un coup |
| `undo()` | Annule le dernier coup, recalcule `highScore` et `nibsCount` depuis l'historique restant |
| `endGame(winner)` | Calcule skunk, met à jour séries/records, pousse dans `stats.games` |
| `openEditGame(idx)` / `saveEditGame()` | Modale de correction : scores, crib, high score, nibs |
| `deleteGame()` | Retire une partie, recalcule séries + records + nibs |
| `exportData()` / `importData(event)` | Sauvegarde/restauration JSON manuelle (voir section 7) |
| `recalcNibs()` / `recalcEverBest()` / `recalcStreaks()` | Recalculent les totaux depuis `stats.games` |

---

## 6. Quirks mobile résolus (⚠️ lire avant de toucher au CSS de layout)

Deux bugs tenaces sur **Chrome Android/iOS** ont été corrigés — ne pas réintroduire les
causes en modifiant le layout sans comprendre pourquoi :

1. **`position: sticky` cassé dans un conteneur `display: flex`**
   Le `<body>` a délibérément **pas** de `display: flex`. Chrome a un bug connu où les
   éléments `sticky` à l'intérieur d'un parent flex arrêtent de coller. Le centrage se
   fait via `max-width` + `margin: auto` sur `.main` à la place.

2. **Hauteur d'écran incorrecte pour les overlays `position: fixed`**
   La barre d'outils dynamique de Chrome mobile (qui apparaît/disparaît au scroll) fausse
   les calculs `100vh`/`100dvh`, coupant le bas d'éléments comme le clavier numérique.
   Fix : une variable CSS `--app-height` est maintenue à jour en JS via
   `window.visualViewport`, et `.numpad-overlay` l'utilise au lieu de `vh`/`dvh`.

3. **Repaint initial manqué au premier chargement**
   Sur certains chargements, le header (pourtant `sticky`) n'apparaît qu'après un scroll.
   Un forçage de repaint (`window.scrollTo(0,1)` puis retour à 0) est déclenché après
   l'événement `load` + deux `requestAnimationFrame`, pour laisser Chrome stabiliser son UI
   avant de forcer le rendu.

4. **Poids des images inline**
   Les icônes "nibs" (valet de carte) sont des JPEG en base64 inline, redimensionnées à
   80×80px et compressées (~4.6 Ko chacune). Une image haute résolution non compressée ici
   a déjà causé des ralentissements de rendu qui aggravaient les bugs 2 et 3. **Ne jamais
   coller une image non compressée directement dans le HTML** — toujours redimensionner à
   la taille d'affichage réelle avant d'encoder en base64.

---

## 7. Export / Import (sauvegarde manuelle)

En attendant un vrai backend (section 8), l'app permet une sauvegarde manuelle :

- **📤 Exporter** (onglet Stats) télécharge `crib-backup-AAAA-MM-JJ.json` contenant
  `{ exportedAt, version, stats, state }`.
- **📥 Importer** relit ce fichier et **remplace entièrement** `stats` (avec confirmation).

Ce JSON est structuré pour matcher un futur schéma SQLite sans transformation majeure —
voir section 8.

---

## 8. Roadmap : migration vers backend FastAPI + SQLite

Prévu mais **pas encore implémenté**. Stack cible :

- **Nginx** sert `index.html` en statique (inchangé).
- **FastAPI** expose une API REST.
- **SQLite** stocke les données côté serveur.
- **`localStorage` reste actif comme cache offline** — l'app doit fonctionner sans réseau
  et synchroniser au retour de connexion.

### Endpoints prévus

```
POST /api/games        ← enregistrer une partie
GET  /api/games         ← historique complet
GET  /api/stats         ← stats agrégées
POST /api/sync          ← envoi en lot des parties en attente (mode offline)
```

### Tables SQLite prévues

```sql
games (id, date, winner, loser, win_score, lose_score,
       skunk, initial_dealer, nibs_dan, nibs_gen,
       high_score_dan, high_score_gen)

-- stats.dan / stats.gen sont dérivées de games à la volée côté serveur aussi,
-- même principe que le front (voir section 3) — ne PAS stocker de totaux
-- cumulés directement en base, les recalculer depuis `games`.
```

### Logique de sync offline-first

```
Partie jouée
  → toujours écrite dans localStorage
  → si serveur joignable → POST /api/games immédiat
  → sinon → marquée "en attente" localement
  → bouton "Sync" manuel (ou auto au retour réseau) → POST /api/sync avec le lot
```

Le JSON d'export (section 7) sert de pont : c'est déjà le format à envoyer à l'API une fois
le backend prêt, migration sans réécriture de données.

---

## 9. Déploiement

Le dépôt est configuré en GitHub Pages, branche `main`, racine `/`.

Pour déployer une modification :
1. Éditer `index.html`.
2. Commit + push sur `main`.
3. GitHub Pages redéploie automatiquement (1-2 min).
4. **Vider le cache navigateur** avant de tester sur mobile — Chrome cache agressivement
   les fichiers statiques, un simple "reload" ne suffit pas toujours.

Aucune étape de build, aucun `npm install` — le fichier poussé est directement servi tel quel.
