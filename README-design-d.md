# rcpilot — Design D drop-in

Unzip this at the root of your rcpilot checkout. It will place three files:

  pilot/static/css/theme-patch.css      (new)
  pilot/static/js/design-d.js           (new)
  pilot/static/index.html               (replaces existing)

The replaced index.html is the current upstream file with two lines added:

  <head>  <link rel="stylesheet" href="/static/css/theme-patch.css" />
  <body>  <script src="/static/js/design-d.js"></script>     (before </body>)

To install:

  unzip rcpilot-design-d.zip
  fish restart.fish

To revert:

  git checkout -- pilot/static/index.html
  rm pilot/static/css/theme-patch.css pilot/static/js/design-d.js
  fish restart.fish
