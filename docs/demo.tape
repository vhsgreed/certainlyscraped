# Terminal demo for the README, rendered by .github/workflows/demo.yml with
# VHS (https://github.com/charmbracelet/vhs) against the live sandbox site.
# Locally: `vhs docs/demo.tape && cp /tmp/certainlyscraped-demo.gif docs/demo.gif`
# (needs vhs, ttyd, ffmpeg and certainlyscraped on PATH).
Output "/tmp/certainlyscraped-demo.gif"

Set Shell "bash"
Set FontSize 15
Set Width 1180
Set Height 720
Set Padding 18
Set TypingSpeed 35ms
Set Theme "Catppuccin Mocha"

Hide
Type "cd $(mktemp -d) && clear" Enter
Show

Type "# 1. point it at a listing page: it probes, guesses a config, previews it" Enter
Type "cscrape init https://books.toscrape.com/catalogue/page-1.html --name books" Enter
Sleep 5s

Type "clear" Enter
Type "# 2. test or tweak one selector" Enter
Type `cscrape try https://books.toscrape.com/ "p.price_color" --in "article.product_pod" --limit 3` Enter
Sleep 4s

Type "clear" Enter
Type "# 3. run it: rows out, row count verified, one report line per page" Enter
Type "cscrape run books.yaml --out - --report report.jsonl 2>/dev/null | head -6" Enter
Sleep 16s

Type "clear" Enter
Type "# 4. the per-page outcome: what it got, how many attempts, which rungs" Enter
Type "head -2 report.jsonl" Enter
Sleep 4s
