# capkit-maker

Turns a capacitor BOM into an orderable recap kit. For every line it finds in-stock replacement
aluminum electrolytics, ranks them by rated life, brand, ESR and price, and writes a results file
(top 3 picks per line, with warnings) plus a cart file you can upload to Mouser or DigiKey.

Specs come from DigiKey's parametric search; prices, stock and part numbers come from Mouser
(default) or DigiKey. Mouser's API has no spec data, so DigiKey is always used to find and filter
candidates.

- One file, `cap_finder.py`, Python 3.8+, standard library only.
- Results are cached for 24 hours, so reruns (for example after you add measured sizes) cost no API calls.

## Setup

### 1. API keys

**DigiKey** (always needed):

1. Sign in at <https://developer.digikey.com> with your DigiKey account.
2. Create an organization, then a **Production** app (sandbox returns fake data).
3. Products: tick **Product Information V4** only.
4. OAuth callback: `https://localhost` (unused; search uses client credentials).
5. Copy the Client ID and Client Secret.

**Mouser** (needed for the default `--vendor mouser`):

1. Sign in to My Mouser and request a **Search API** key from the API hub
   (<https://www.mouser.com/api-hub/>).

### 2. Key files

Put the keys next to `cap_finder.py`, readable only by you:

```sh
cat > .digikey.env <<'EOF'
DIGIKEY_CLIENT_ID=your-client-id
DIGIKEY_CLIENT_SECRET=your-client-secret
EOF
echo 'MOUSER_API_KEY=your-key' > .mouser.env
chmod 600 .digikey.env .mouser.env
```

Environment variables with the same names work too and take precedence. Both files are in
`.gitignore`; never commit them.

## Quick start

```sh
# single part, printed to the terminal
python3 cap_finder.py 470uF 25V --qty 2 --dia 10 --max-height 20

# start a BOM (header row only; refuses to overwrite an existing file)
python3 cap_finder.py --new-bom "My BOM.csv"

# whole BOM: writes "My BOM - Mouser results.csv" and "My BOM - Mouser cart.csv" next to the BOM
python3 cap_finder.py --bom "My BOM.csv" --consolidate --life-cap 10000

# same, priced and bought at DigiKey
python3 cap_finder.py --bom "My BOM.csv" --vendor digikey

# offline tests
python3 test_cap_finder.py
```

## BOM format

A BOM is a CSV with one row per distinct capacitor. This is [`example_bom.csv`](example_bom.csv):

```csv
Capacitance (uF),Voltage (V),Type,Qty,Diameter (mm),Lead Spacing (mm),Max Height (mm),MPN
10,16,Radial,4,5,2,,
22,16,Radial,3,5,2,,
22,10,Radial,1,5,2,,
47,25,Radial,2,,,,
100,35,Radial,1,8,3.5,14,
470,25,Radial,2,10,5,20,
2200,35,Radial,1,16,7.5,32,
10,16,Bipolar,1,,,,
0.47,100,Film,1,,,,MKS2D034701E00KSSD
1000,200,Large can,1,35,10,45,
```

What each kind of row asks for:

- `10,16,Radial,4,5,2,,` – four 10 µF 16 V caps in a 5 mm can with 2 mm lead spacing.
- `47,25,Radial,2,,,,` – two 47 µF 25 V caps, any size. Blank size columns mean no limit.
- `470,25,Radial,2,10,5,20,` – a 10 mm can with 5 mm lead spacing, at most 20 mm tall.
- `22,16,...` and `22,10,...` – the same value in the same 5 mm can. With `--consolidate`
  the 10 V line is folded into the 16 V part, so you buy one part number.
- `10,16,Bipolar,1,,,,` – `Type` set to `Bipolar` searches bi-polar parts (85 °C rated ones
  allowed). Any other `Type` (`Radial`, `Film`, `Large can`, blank) is just a label.
- `0.47,100,Film,1,,,,MKS2D034701E00KSSD` – an `MPN` means "buy exactly this part": no search,
  and the size columns are ignored. Here a WIMA film cap replaces a small electrolytic.
- `1000,200,Large can,1,35,10,45,` – a snap-in can with its measured diameter, lead spacing and height.

Rules:

- Only `Capacitance (uF)`, `Voltage (V)` and `Qty` are required. This is a complete BOM:
  ```csv
  Capacitance (uF),Voltage (V),Qty
  470,25,2
  ```
- Column names must match exactly, in any order. Optional columns can be blank or left out.
- Extra columns are ignored, so you can keep notes in the file, e.g. a `Refs` column with the
  board designators.
- Voltage is matched exactly. For a higher-voltage substitute, use the `MPN` column.
- Size limits are applied locally to the cached search results, so adding measurements later costs
  no DigiKey calls.
- `python3 cap_finder.py --new-bom FILE` writes a BOM with every column header.

## Options

| Option | Mode | Effect |
|---|---|---|
| `CAP VOLTS` | single | e.g. `470uF 25V` |
| `--qty N` | single | pieces needed (default 1) |
| `--bipolar` | single | search bi-polar parts |
| `--dia MM`, `--spacing MM`, `--max-height MM` | single | size limits (BOM mode uses the columns) |
| `--bom FILE` | BOM | search every line of a BOM CSV |
| `--consolidate` | BOM | fold lines into a higher-voltage line of the same value and footprint (see below) |
| `--life-cap HOURS` | both | rated life beyond this counts as equal (see ranking) |
| `--smd` | both | surface mount instead of radial through-hole |
| `--vendor mouser\|digikey` | both | where to price and buy (default `mouser`) |
| `--fresh` | both | ignore the cache; use right before ordering |
| `--new-bom FILE` | — | write an empty BOM and exit |

## How parts are picked

**Candidates.** For each line: DigiKey in-stock parts (marketplace sellers excluded) with the exact
capacitance and voltage, through-hole radial (snap-in included, axial excluded) or `--smd`, rated
105 °C or higher (85 °C for bi-polar, which are rarely 105 °C), polarity matching `Type`, within
the size limits. In Mouser mode, parts DigiKey marks obsolete are dropped.

**Life.** Rated life is normalized to 105 °C: wet electrolytics and hybrid polymers double per
10 °C (2000 h @ 125 °C counts as 8000 h), solid polymers gain 10x per 20 °C.

**Life target.** The longest life available on the line, but never more than the life cap. Above
the target, more life doesn't count. In BOM mode the cap defaults to 2x the best life of the BOM's
weakest polar line (its weakest link; bi-polar lines are left out), or set it with `--life-cap`.
Single-part searches have no cap unless you give one.

**Order.** Preferred brands (Nichicon, Panasonic, Rubycon, Chemi-Con, Würth Elektronik) before
others. Among parts that reach the target: Nichicon first, then lowest ESR (parts without ESR data
after those with), then lowest cost; parts costing 1.5x or more than the cheapest qualifying part
go last. Parts short of the target follow, longest life first. The top 3 show one part per
series and size, so you don't get three packagings of the same can.

**Groups.** Aluminum electrolytic and aluminum polymer parts are ranked separately. Polymer picks
are shown for reference only and never go in the cart; their much lower ESR can upset regulator
or power-supply stability.

**Warnings** on the #1 pick:

- another candidate has half the ESR or less
- another candidate has 1.5x the ripple current or more (same test frequency)
- #1 costs at least 1.5x and $1 more than #2/#3
- #1 is 3 mm or more taller than #2
- buying extra pieces because a price break makes the total cheaper

**Quantity.** The cheapest way to get at least `Qty` pieces: it buys more only when a price break
lowers the total (9 needed, 10 cheaper → buy 10). Full reels and big minimum-order packs are skipped.

## Consolidation (`--consolidate`)

A line is folded into a higher-voltage line of the same capacitance and polarity when that line's
#1 pick has the same can diameter and lead spacing as this line's #1 pick, fits this line's
`Max Height`, and has stock for both. Its quantity moves to the higher-voltage line and it leaves
the cart. It mostly helps small caps, where higher voltages often come in the same can. It saves
part numbers, not always money.

## Vendors

**Mouser (default).** DigiKey finds and ranks candidates; then each line's top 5 part numbers are
looked up at Mouser, all lines batched 10 per call (Mouser's maximum). A line with nothing in stock
at Mouser widens 10 part numbers at a time, up to 50. The final ranking, price cap, quantities and
cart all use Mouser prices and stock.

**DigiKey.** DigiKey does both search and pricing.

## Output

`<BOM> - <Vendor> results.csv`: for each line and group, the top 3 picks with life, ESR, ripple,
size, lead spacing, stock, vendor part number, quantity to buy, cost, warnings and product URL.
Lines with no match say so.

`<BOM> - <Vendor> cart.csv`: the #1 electrolytic (or the BOM's `MPN`) per line, one row per part
number (lines buying the same part are combined).

- Mouser columns: `Mouser Part Number, Quantity, Customer Part Number`
- DigiKey columns: `Digi-Key Part Number, Quantity, Customer Reference`

Upload it with the vendor's BOM or cart upload tool and map the columns if it asks.

## Caching and API limits

DigiKey search results and Mouser answers are saved in `.caps_cache/` next to the script and
reused for 24 hours. Use `--fresh` before ordering to get live prices and stock. Old files are
ignored but not deleted; remove the folder to clear it.

| API | Limits | Typical use |
|---|---|---|
| DigiKey | 120/min, 1000/day | about 3 calls per BOM line, fetched 4 lines at a time |
| Mouser | 30/min, 1000/day | about 20 calls for a 30-line BOM (10 part numbers per call) |

A 30-line BOM takes about 2 minutes with an empty cache and a few seconds after that. Each run
prints its API call counts.

Tuning constants (life cap factor, warning thresholds, Mouser top-N, cache lifetime, worker count)
are at the top of `cap_finder.py`.

## Limitations

- Only parts DigiKey has in stock are considered, even in Mouser mode.
- DigiKey returns at most 300 results per search; larger searches are split by manufacturer, and a
  single manufacturer with more than 300 matches is cut at 300.
- Voltage is matched exactly. Use the `MPN` column for a higher-voltage or film substitute.
- Dimensions are only as good as DigiKey's data; check large cans and tall picks against the board.
- The cart column names follow each vendor's export format but haven't been tested against every
  upload page.
