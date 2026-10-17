#!/usr/bin/env python3
"""Find replacement aluminum capacitors at Mouser (default) or DigiKey, ranked by rated life.

Specs always come from DigiKey's parametric search (Mouser's API has no spec data); candidates are parts
DigiKey has in stock, marketplace sellers excluded. With --vendor mouser (the default) the best-ranked
candidates are then priced at Mouser, and Mouser prices, stock, part numbers and cart are used; parts
Mouser can't sell drop out. With --vendor digikey, DigiKey does both.

Mouser pricing: each line's top 5 candidates are looked up, all lines batched 10 part numbers per call; a
line with nothing in stock at Mouser widens 10 more at a time (up to 50).
Cache: DigiKey results and Mouser answers are saved in .caps_cache next to this script and reused for
24 hours, so a rerun (e.g. after adding sizes, which are applied locally) costs no API calls.
Use --fresh to ignore the cache, e.g. right before ordering.

Single part (prints to stdout):
    python3 cap_finder.py 470uF 25V [--qty 3] [--bipolar] [--smd] [--dia 10] [--spacing 5] [--max-height 20]
Whole BOM (writes '<bom> - Mouser results.csv' and '<bom> - Mouser cart.csv' next to the BOM):
    python3 cap_finder.py --bom "Sony PVM 2950Q Cap Kit BOM.csv" [--smd] [--consolidate] [--vendor digikey]
Empty BOM to fill in (header row only; won't overwrite an existing file):
    python3 cap_finder.py --new-bom "My BOM.csv"

BOM CSV format: a header row, then one row per distinct cap. Column names must match exactly;
extra columns are ignored, and optional columns can be left out or left blank.
    Capacitance (uF)    required  e.g. 470 or 0.47
    Voltage (V)         required  rated voltage, e.g. 25
    Qty                 required  pieces needed
    Type                optional  "Bipolar" searches bi-polar parts; anything else (Radial, Large can, blank) is polar
    Diameter (mm)       optional  only parts with this can diameter
    Lead Spacing (mm)   optional  only parts with this lead spacing
    Max Height (mm)     optional  only parts this tall or shorter
    MPN                 optional  buy exactly this manufacturer part number, no search (e.g. a WIMA MKS2 film
                                  cap in place of a small electrolytic); the size columns are ignored
Example:
    Capacitance (uF),Voltage (V),Type,Qty,Diameter (mm),Lead Spacing (mm),Max Height (mm),MPN
    470,25,Radial,2,10,5,20,
    10,16,Bipolar,1,,,,
    0.47,100,Film,1,,,,MKS2D034701E00KSSD

--consolidate: a line is folded into a higher-voltage line of the same capacitance and polarity when
that line's #1 pick has the same can diameter and lead spacing as this line's #1 pick, fits this line's
Max Height, and has stock for both. Its qty moves to the higher-voltage line and it leaves the cart.
Mostly helps small caps, where higher voltages often come in the same can.

Credentials: DIGIKEY_CLIENT_ID / DIGIKEY_CLIENT_SECRET (always) and MOUSER_API_KEY (Mouser mode), as env
vars or in .digikey.env / .mouser.env next to this script.

Ranking (per line; electrolytic and polymer separately): in stock, rated >=105C (>=85C for bi-polar),
exact capacitance and voltage, within the size limits. Preferred brands (Nichicon, Panasonic, Rubycon, Chemi-Con, Würth) come
before others. Rated life (normalized to 105C) counts up to a target: the longest life available, but no
more than the life cap. Parts that reach the target are ordered Nichicon first, then lowest ESR, then
price, and any costing >=1.5x the cheapest of them go last. Parts short of the target follow, longest
life first. The cart file gets the #1 electrolytic per line.

Life cap: --life-cap HOURS, or in BOM mode by default 2x the lowest best-available life among the polar
lines (the kit's weakest link; bi-polar lines are left out since bi-polar parts are short-lived anyway).
Single-part searches have no cap unless you give one.
"""
import argparse, csv, hashlib, json, os, re, sys, threading, time, urllib.error, urllib.parse, urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

API = "https://api.digikey.com"
MOUSER_API = "https://api.mouser.com/api"
MOUSER_GAP = 2.1  # seconds between Mouser calls; Mouser allows 30 per minute
MOUSER_TOP = 5    # shortcut: only each line's 5 best-ranked candidates get Mouser prices; raise for wider price coverage
MOUSER_MAX = 50   # a line with nothing in stock at Mouser widens 10 part numbers at a time up to this many
DK_GAP, DK_WORKERS = 0.5, 4  # DigiKey allows 120 calls per minute; BOM lines are fetched this many at a time
CACHE = Path(__file__).with_name(".caps_cache")  # DigiKey results and Mouser answers, reused for CACHE_HOURS
CACHE_HOURS = 24
PREFERRED = {493: "Nichicon", 10: "Panasonic", 1189: "Rubycon", 565: "Chemi-Con", 732: "Würth Elektronik"}
NICHICON = 493                                     # first among preferred brands once life is enough
LIFE_FACTOR = 2                                    # BOM life cap = this x the weakest line's best life
TALLER_MM = 3                                      # warn if #1 is this much taller than #2
GROUPS = {"Electrolytic": "58", "Polymer": "69"}   # DigiKey category ids
MOUNT = {"radial": "411897", "smd": "409393"}      # Mounting Type value ids: Through Hole / Surface Mount
P_CAP, P_VOLT, P_MOUNT = 2049, 2079, 69            # DigiKey parameter ids
TOP = 3
ESR_RATIO, RIPPLE_RATIO = 0.5, 1.5                 # warn if another part has <=0.5x ESR or >=1.5x ripple
PRICE_RATIO, PRICE_MIN_DIFF = 1.5, 1.00            # warn if #1 costs >=1.5x and >=$1 more than #2/#3
MAX_RESULTS = 300  # DigiKey serves at most 300 results per search (Offset + Limit <= 300)
BOM_COLUMNS = ["Capacitance (uF)", "Voltage (V)", "Type", "Qty", "Diameter (mm)", "Lead Spacing (mm)",
               "Max Height (mm)", "MPN"]


def fmt(x):
    return f"{x:g}"


def load_env(filename, *names):
    """Values of names from env vars, else from filename next to this script."""
    env = Path(__file__).with_name(filename)
    if env.exists():
        for line in env.read_text().splitlines():
            k, _, v = line.partition("=")
            if v:
                os.environ.setdefault(k.strip(), v.strip())
    try:
        return [os.environ[n] for n in names]
    except KeyError:
        sys.exit(f"Set {' and '.join(names)}, or create {filename} next to this script.")


def norm(mpn):
    return re.sub(r"[^A-Z0-9]", "", mpn.upper())


def cached(path, fresh):
    """Cached JSON at path if it's younger than CACHE_HOURS (and fresh isn't set), else None."""
    if not fresh and path.exists() and time.time() - path.stat().st_mtime < CACHE_HOURS * 3600:
        return json.loads(path.read_text())
    return None


def save(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data))


class DigiKey:
    def __init__(self, fresh=False):
        self.id, self.secret = load_env(".digikey.env", "DIGIKEY_CLIENT_ID", "DIGIKEY_CLIENT_SECRET")
        self.token, self.calls, self.hits, self.fresh = None, 0, 0, fresh
        self.lock, self.next_slot = threading.Lock(), 0.0

    def _auth(self):
        data = urllib.parse.urlencode({"client_id": self.id, "client_secret": self.secret,
                                       "grant_type": "client_credentials"}).encode()
        self.token = json.load(urllib.request.urlopen(API + "/v1/oauth2/token", data))["access_token"]

    def search(self, body):
        path = CACHE / "digikey" / (hashlib.sha1(json.dumps(body, sort_keys=True).encode()).hexdigest() + ".json")
        d = cached(path, self.fresh)
        if d is not None:
            with self.lock:
                self.hits += 1
            return d
        d = self._request(body)
        save(path, d)
        return d

    def _request(self, body):
        """One rate-limited search call; safe to use from several threads."""
        for _ in range(4):
            with self.lock:
                if not self.token:
                    self._auth()
                token, slot = self.token, max(time.time(), self.next_slot)
                self.next_slot, self.calls = slot + DK_GAP, self.calls + 1
            time.sleep(max(0.0, slot - time.time()))
            req = urllib.request.Request(API + "/products/v4/search/keyword", json.dumps(body).encode(), {
                "Authorization": "Bearer " + token, "X-DIGIKEY-Client-Id": self.id,
                "X-DIGIKEY-Locale-Site": "US", "X-DIGIKEY-Locale-Language": "en",
                "X-DIGIKEY-Locale-Currency": "USD", "Content-Type": "application/json"})
            try:
                return json.load(urllib.request.urlopen(req))
            except urllib.error.HTTPError as e:
                if e.code == 401:  # token lasts 10 minutes
                    with self.lock:
                        if self.token == token:
                            self.token = None
                elif e.code == 429:
                    time.sleep(int(e.headers.get("Retry-After", 10)))
                else:
                    sys.exit(f"DigiKey API error {e.code}: {e.read()[:300].decode(errors='replace')}")
        sys.exit("DigiKey API: gave up after retries")

    def fetch(self, category, cap, volts, mount):
        """In-stock, non-marketplace DigiKey products of this value, voltage and mounting."""
        filters = [{"ParameterId": P_CAP, "FilterValues": [{"Id": f"{fmt(cap)} µF"}]},
                   {"ParameterId": P_VOLT, "FilterValues": [{"Id": f"{fmt(volts)} V"}]},
                   {"ParameterId": P_MOUNT, "FilterValues": [{"Id": MOUNT[mount]}]}]
        opts = {"SearchOptions": ["InStock"], "MarketPlaceFilter": "ExcludeMarketPlace",
                "ParameterFilterRequest": {"CategoryFilter": {"Id": category}, "ParameterFilters": filters}}
        first = self.search({"Keywords": "", "Limit": 50, "Offset": 0, "FilterOptionsRequest": opts})
        if first.get("ProductsCount", 0) <= MAX_RESULTS:  # DigiKey omits the count when nothing matches
            return self._pages(opts, first)
        # too many for one search: split by manufacturer into batches DigiKey can serve whole
        batches, n = [[]], 0
        for m in sorted(first["FilterOptions"]["Manufacturers"], key=lambda m: -m["ProductCount"]):
            if batches[-1] and n + m["ProductCount"] > MAX_RESULTS:
                batches.append([])
                n = 0
            batches[-1].append({"Id": m["Id"]})
            n += m["ProductCount"]
        return [p for b in batches for p in self._pages({**opts, "ManufacturerFilter": b})]

    def _pages(self, opts, first=None):
        # shortcut: a single manufacturer with more than MAX_RESULTS matches gets cut at MAX_RESULTS
        products, d = [], first
        for offset in range(0, MAX_RESULTS, 50):
            d = d or self.search({"Keywords": "", "Limit": 50, "Offset": offset, "FilterOptionsRequest": opts})
            products += d["Products"]
            if not d["Products"] or len(products) >= d["ProductsCount"]:
                break
            d = None
        return products


class Mouser:
    def __init__(self, fresh=False):
        self.key, = load_env(".mouser.env", "MOUSER_API_KEY")
        self.calls, self.hits, self.last, self.cache, self.fresh = 0, 0, 0.0, {}, fresh

    def parts(self, mpns):
        """Exact part-number lookup: {normalized MPN: [Mouser parts], [] if Mouser has none}.
        Only MPNs not looked up before (this run or in the disk cache) are sent, 10 per call (Mouser's maximum)."""
        for m in mpns:
            k = norm(m)
            if k not in self.cache and (hit := cached(CACHE / "mouser" / f"{k}.json", self.fresh)) is not None:
                self.cache[k] = hit
                self.hits += 1
        todo = list(dict.fromkeys(m for m in mpns if norm(m) not in self.cache))
        for i in range(0, len(todo), 10):
            chunk = todo[i:i + 10]
            body = {"SearchByPartRequest": {"mouserPartNumber": "|".join(chunk), "partSearchOptions": "Exact"}}
            for _ in range(3):
                time.sleep(max(0.0, MOUSER_GAP - (time.time() - self.last)))
                self.last, self.calls = time.time(), self.calls + 1
                req = urllib.request.Request(f"{MOUSER_API}/v1/search/partnumber?apiKey={self.key}",
                                             json.dumps(body).encode(), {"Content-Type": "application/json"})
                try:
                    d = json.load(urllib.request.urlopen(req))
                    break
                except urllib.error.HTTPError as e:
                    if e.code != 429:
                        sys.exit(f"Mouser API error {e.code}: {e.read()[:300].decode(errors='replace')}")
                    time.sleep(60)
            else:
                sys.exit("Mouser API: gave up after retries")
            if d.get("Errors"):
                sys.exit(f"Mouser API error: {d['Errors']}")
            for m in chunk:
                self.cache.setdefault(norm(m), [])
            for p in (d.get("SearchResults") or {}).get("Parts") or []:
                self.cache.setdefault(norm(p["ManufacturerPartNumber"]), []).append(p)
            for m in chunk:
                save(CACHE / "mouser" / f"{norm(m)}.json", self.cache[norm(m)])
        return {norm(m): self.cache[norm(m)] for m in mpns}


# --- parsing DigiKey parameter text ---

def hz(x):
    return f"{fmt(x / 1000)} kHz" if x >= 1000 else f"{fmt(x)} Hz"


def mm(s):
    m = re.search(r"\(([\d.]+)mm", s or "")
    return float(m.group(1)) if m else None


def ohms(s):
    m = re.search(r"([\d.]+)\s*(m?)Ohm", s or "")
    return float(m.group(1)) / (1000 if m.group(2) else 1) if m else None


def amps(s):
    m = re.search(r"([\d.]+)\s*(m?)A\s*@\s*([\d.]+)\s*(k?)Hz", s or "")
    if not m:
        return None
    return float(m.group(1)) / (1000 if m.group(2) else 1), float(m.group(3)) * (1000 if m.group(4) else 1)


def max_temp(s):
    temps = re.findall(r"(-?\d+)°C", s or "")
    return int(temps[-1]) if temps else None


def life105(s, solid_polymer):
    m = re.search(r"([\d,]+)\s*Hrs\s*@\s*(\d+)", s or "")
    if not m:
        return 0
    hours, temp = float(m.group(1).replace(",", "")), int(m.group(2))
    # wet electrolytic and hybrid: life doubles per 10C cooler; solid polymer: x10 per 20C cooler
    return hours * (10 ** ((temp - 105) / 20) if solid_polymer else 2 ** ((temp - 105) / 10))


def best_buy(variations, need):
    """Cheapest way to get >= need pieces; buys more when a price break makes the total lower."""
    best = None
    for v in variations:
        # skip marketplace sellers, and full reels / big bulk packs that make no sense for a kit
        if v.get("MarketPlace") or v.get("MinimumOrderQuantity", 1) > max(need, 10):
            continue
        breaks = sorted((b["BreakQuantity"], b["UnitPrice"]) for b in v.get("StandardPricing") or [])
        for q in sorted({need} | {b for b, _ in breaks if b > need}):
            if q < v.get("MinimumOrderQuantity", 1) or q > v.get("QuantityAvailableforPackageType", 0) \
                    or q % (v.get("Mult") or 1):
                continue
            units = [u for b, u in breaks if b <= q]
            if not units:
                continue
            total = q * units[-1] + (v.get("DigiReelFee") or 0)
            if best is None or total < best["total"] - 1e-9:
                best = {"pn": v["DigiKeyProductNumber"], "packaging": v["PackageType"]["Name"],
                        "buy": q, "unit": units[-1], "total": total}
    return best


def mouser_variation(p):
    """A Mouser part in the DigiKey variation shape best_buy reads (DigiKeyProductNumber holds the Mouser PN)."""
    money = lambda s: float(re.sub(r"[^\d.]", "", s))
    pack = next((a["AttributeValue"] for a in p.get("ProductAttributes") or [] if a["AttributeName"] == "Packaging"), "")
    return {"DigiKeyProductNumber": p["MouserPartNumber"], "PackageType": {"Name": pack},
            "MinimumOrderQuantity": int(p.get("Min") or 1), "Mult": int(p.get("Mult") or 1),
            "QuantityAvailableforPackageType": int(p.get("AvailabilityInStock") or 0),
            "StandardPricing": [{"BreakQuantity": b["Quantity"], "UnitPrice": money(b["Price"])}
                                for b in p.get("PriceBreaks") or []]}


def candidates(products, need, bipolar, mount, dia, spacing, max_height=None, priced=True):
    """Parts that pass the filters; priced=False leaves DigiKey pricing out (another vendor prices them)."""
    out = []
    for p in products:
        par = {x["ParameterText"]: x["ValueText"] for x in p["Parameters"]}
        if not priced and (p.get("ProductStatus") or {}).get("Status") == "Obsolete":
            continue
        if (max_temp(par.get("Operating Temperature")) or 0) < (85 if bipolar else 105):  # few bi-polars are 105C
            continue
        if ("bi" in par.get("Polarization", "Polar").lower()) != bipolar:
            continue
        if mount == "radial" and not par.get("Package / Case", "").startswith("Radial"):
            continue  # drops axial
        d, ls, h = mm(par.get("Size / Dimension")), mm(par.get("Lead Spacing")), mm(par.get("Height - Seated (Max)"))
        if (dia and (d is None or abs(d - dia) > 0.05)) or (spacing and (ls is None or abs(ls - spacing) > 0.05)) \
                or (max_height and (h is None or h > max_height + 0.05)):
            continue
        buy = best_buy(p.get("ProductVariations") or [], need) if priced else \
            {"pn": "", "packaging": "", "buy": need, "unit": 0.0, "total": 0.0}
        if not buy:
            continue
        out.append({**buy,
                    "brand": p["Manufacturer"]["Name"], "brand_id": p["Manufacturer"]["Id"],
                    "preferred": p["Manufacturer"]["Id"] in PREFERRED,
                    "mpn": p["ManufacturerProductNumber"], "series": (p.get("Series") or {}).get("Name", ""),
                    "type": par.get("Type", ""), "life": par.get("Lifetime @ Temp.", "-"),
                    "life105": life105(par.get("Lifetime @ Temp."), par.get("Type") == "Polymer"),
                    "maxtemp": max_temp(par.get("Operating Temperature")),
                    "esr": ohms(par.get("ESR (Equivalent Series Resistance)")) or ohms(par.get("Impedance")),
                    "ripple_hf": amps(par.get("Ripple Current @ High Frequency")),
                    "ripple_lf": amps(par.get("Ripple Current @ Low Frequency")),
                    "dia": d, "height": h, "spacing": ls,
                    "package": par.get("Package / Case", ""), "stock": p.get("QuantityAvailable", 0),
                    "url": p.get("ProductUrl", ""), "variations": p.get("ProductVariations") or []})
    return out


def reach(pool):
    """Best life available: among preferred brands if any are in the pool."""
    ref = [c for c in pool if c["preferred"]] or pool
    return max((c["life105"] for c in ref), default=0)


def rank(pool, life_cap=None):
    """(top picks, life target), one pick per series+size."""
    ranked, target = order(pool, life_cap)
    seen, top = set(), []
    for c in ranked:
        k = (c["brand"], c["series"], c["dia"], c["height"], c["spacing"])
        if k not in seen:
            seen.add(k)
            top.append(c)
        if len(top) == TOP:
            break
    return top, target


def order(pool, life_cap=None):
    """(pool sorted best first, life target); see Ranking in the module doc."""
    if not pool:
        return [], None
    target = min(reach(pool), life_cap) if life_cap else reach(pool)
    ref = [c for c in pool if c["preferred"]] or pool
    cheapest = min(c["total"] for c in ref if c["life105"] >= target)

    def key(c):
        enough = c["life105"] >= target
        return (not c["preferred"], not enough, 0 if enough else -c["life105"],
                c["total"] >= PRICE_RATIO * cheapest, c["brand_id"] != NICHICON,
                c["esr"] is None, c["esr"] or 0, c["total"])

    return sorted(pool, key=key), target


def warnings(top, pool):
    if not top:
        return ["no in-stock match"]
    pick, w = top[0], []
    others = [c for c in pool if c["mpn"] != pick["mpn"]]
    if pick["esr"]:
        lo = min((c for c in others if c["esr"]), key=lambda c: c["esr"], default=None)
        if lo and lo["esr"] <= ESR_RATIO * pick["esr"]:
            w.append(f"lower ESR/Z: {lo['brand']} {lo['mpn']} {lo['esr'] * 1000:g} mOhm vs {pick['esr'] * 1000:g}")
    for k in ("ripple_hf", "ripple_lf"):  # compare at the same frequency only
        if pick[k]:
            same = [c for c in others if c[k] and c[k][1] == pick[k][1]]
            hi = max(same, key=lambda c: c[k][0], default=None)
            if hi and hi[k][0] >= RIPPLE_RATIO * pick[k][0]:
                w.append(f"higher ripple: {hi['brand']} {hi['mpn']} {hi[k][0]:g} A vs {pick[k][0]:g} A "
                         f"@ {hz(pick[k][1])}")
            break
    cheapest_other = min((c["total"] for c in top[1:]), default=None)
    if cheapest_other and pick["total"] >= PRICE_RATIO * cheapest_other \
            and pick["total"] - cheapest_other >= PRICE_MIN_DIFF:
        w.append(f"expensive: ${pick['total']:.2f} vs ${cheapest_other:.2f} for #2/#3")
    if len(top) > 1 and pick["height"] and top[1]["height"] and pick["height"] - top[1]["height"] >= TALLER_MM:
        w.append(f"taller: {fmt(pick['height'])}mm vs {fmt(top[1]['height'])}mm for #2")
    if pick["buy"] > pick.get("need", pick["buy"]):
        w.append(f"buying {pick['buy']} (price break)")
    return w


def finish(pool, life_cap=None):
    top, target = rank(pool, life_cap)
    return {"top": top, "warns": warnings(top, pool), "pool": pool, "target": target}


def pools(dk, cap, volts, need, bipolar, mount, dia, spacing, max_height=None, priced=True):
    """{group: candidate pool} from DigiKey, groups fetched in parallel; priced=False leaves DigiKey prices out."""
    groups = [(g, cat) for g, cat in GROUPS.items() if not (g == "Polymer" and bipolar)]  # no bi-polar polymers
    with ThreadPoolExecutor(len(groups)) as ex:
        fetched = list(ex.map(lambda gc: dk.fetch(gc[1], cap, volts, mount), groups))
    out = {}
    for (group, _), products in zip(groups, fetched):
        out[group] = candidates(products, need, bipolar, mount, dia, spacing, max_height, priced)
        for c in out[group]:
            c["need"] = need
    return out


def mouser_mpns(pool, life_cap=None):
    """Part numbers to price at Mouser, best-ranked first."""
    return list(dict.fromkeys(c["mpn"] for c in order(pool, life_cap)[0]))


def price_all(mouser, jobs, life_cap=None):
    """Mouser-price (pool, need) jobs: each job's top MOUSER_TOP part numbers go out together in one batched
    sweep; jobs with nothing in stock widen 10 at a time (again batched) up to MOUSER_MAX."""
    mpns = [mouser_mpns(pool, life_cap) for pool, _ in jobs]
    limit = [MOUSER_TOP] * len(jobs)
    while True:
        mouser.parts([m for ms, n in zip(mpns, limit) for m in ms[:n]])
        priced = [price_mouser(mouser, pool, need, life_cap, n) for (pool, need), n in zip(jobs, limit)]
        short = [i for i, p in enumerate(priced) if not p and limit[i] < min(len(mpns[i]), MOUSER_MAX)]
        if not short:
            return priced
        for i in short:
            limit[i] += 10


def price_mouser(mouser, pool, need, life_cap=None, n=MOUSER_TOP):
    """Mouser-priced copy of the pool's n best-ranked part numbers; parts Mouser can't sell are left out."""
    ranked, _ = order(pool, life_cap)
    found = mouser.parts(mouser_mpns(pool, life_cap)[:n])
    priced = []
    for c in ranked:
        parts = found.get(norm(c["mpn"]))
        variations = [mouser_variation(p) for p in parts or []]
        buy = best_buy(variations, need)
        if buy:
            priced.append({**c, **buy, "variations": variations, "url": parts[0].get("ProductDetailUrl", ""),
                           "stock": sum(v["QuantityAvailableforPackageType"] for v in variations)})
    return priced


def fixed_part(dk, mouser, mpn, need):
    """[the BOM's own part number, priced at the vendor], or [] if the vendor can't sell it."""
    if mouser:
        parts = mouser.parts([mpn])[norm(mpn)]
        variations = [mouser_variation(p) for p in parts]
        info = {"brand": parts[0]["Manufacturer"], "package": parts[0].get("Description", ""),
                "url": parts[0].get("ProductDetailUrl", "")} if parts else {}
    else:
        d = dk.search({"Keywords": mpn, "Limit": 10, "Offset": 0,
                       "FilterOptionsRequest": {"MarketPlaceFilter": "ExcludeMarketPlace"}})
        p = next((p for p in d.get("Products") or [] if norm(p["ManufacturerProductNumber"]) == norm(mpn)), None)
        variations = (p or {}).get("ProductVariations") or []
        info = {"brand": p["Manufacturer"]["Name"], "package": p["Description"]["ProductDescription"],
                "url": p.get("ProductUrl", "")} if p else {}
    buy = best_buy(variations, need)
    if not buy:
        return []
    return [{**buy, **info, "brand_id": 0, "preferred": False, "mpn": mpn, "series": "", "type": "", "life": "-",
             "life105": 0, "maxtemp": None, "esr": None, "ripple_hf": None, "ripple_lf": None, "dia": None,
             "height": None, "spacing": None, "need": need, "variations": variations,
             "stock": sum(v["QuantityAvailableforPackageType"] for v in variations)}]


def lookup(dk, mouser, cap, volts, need, bipolar, mount, dia, spacing, max_height=None, life_cap=None):
    """{group: {top, warns, pool, target}} for one capacitor; priced at Mouser when mouser is given."""
    found = pools(dk, cap, volts, need, bipolar, mount, dia, spacing, max_height, priced=mouser is None)
    if mouser:
        found = dict(zip(found, price_all(mouser, [(p, need) for p in found.values()], life_cap)))
    return {g: finish(p, life_cap) for g, p in found.items()}


def consolidate(lines):
    """Fold BOM lines into a higher-voltage line of the same value with the same footprint (see --consolidate).
    Mutates lines: a folded line gets "into", its target lists it in "merged". Returns notes."""
    kept, notes = [], []
    for ln in sorted(lines, key=lambda l: -l["volts"]):
        top = ln["result"].get("Electrolytic", {}).get("top")  # BOM-fixed parts have no Electrolytic group
        if not top:
            continue
        mine = top[0]
        for h in sorted(kept, key=lambda k: k["volts"]):  # nearest higher voltage first
            pick = h["result"]["Electrolytic"]["top"][0]
            need = h["need"] + sum(m["need"] for m in h["merged"]) + ln["need"]
            if ((h["cap"], h["bipolar"]) == (ln["cap"], ln["bipolar"]) and h["volts"] > ln["volts"]
                    and mine["dia"] and (pick["dia"], pick["spacing"]) == (mine["dia"], mine["spacing"])
                    and (not ln["max_h"] or (pick["height"] or 1e9) <= ln["max_h"] + 0.05)
                    and pick["stock"] >= need):
                ln["into"] = h
                h["merged"].append(ln)
                notes.append(f"{ln['part']} -> {h['part']} ({fmt(mine['dia'])}mm can)")
                break
        else:
            kept.append(ln)
    for h in kept:
        if not h["merged"]:
            continue
        need = h["need"] + sum(m["need"] for m in h["merged"])
        r = h["result"]["Electrolytic"]
        for c in r["top"]:
            c.update(best_buy(c["variations"], need) or {}, need=need)
        r["warns"] = warnings(r["top"], r["pool"]) + ["includes " + ", ".join(m["part"] for m in h["merged"])]
        for m in h["merged"]:
            m["result"]["Electrolytic"]["warns"].append(f"consolidated into {h['part']}; not in cart")
    return notes


def merge_cart(cart):
    """One cart row per part number: quantities added, references joined."""
    merged = {}
    for pn, qty, ref in cart:
        q, refs = merged.get(pn, (0, []))
        merged[pn] = (q + qty, refs + [ref])
    return [[pn, q, " + ".join(refs)] for pn, (q, refs) in merged.items()]


def life_str(c):
    return f"{c['life105']:,.0f}h" if c["life105"] else "-"


def print_result(title, result):
    print(title)
    for group, r in result.items():
        top, warns = r["top"], r["warns"]
        print(f"\n  {group}" + (f" (life target {r['target']:,.0f}h)" if r["target"] else ""))
        if top:
            print(f"  {'#':<2} {'Brand':<17} {'MPN':<24} {'Life@105':>9} {'Rated':<18} {'ESR/Z':>8} "
                  f"{'Ripple':>13} {'Dia x H':>11} {'LS':>5} {'Stock':>7} {'Buy':>4} {'Total':>8}  Order PN")
        for i, c in enumerate(top, 1):
            esr = f"{c['esr'] * 1000:g}mΩ" if c["esr"] else "-"
            rip = c["ripple_hf"] or c["ripple_lf"]
            rip = f"{rip[0]:g}A@{fmt(rip[1] / 1000)}k" if rip else "-"
            dash = lambda x: fmt(x) if x else "-"
            size = f"{dash(c['dia'])}x{dash(c['height'])}"
            print(f"  {i:<2} {c['brand'][:17]:<17} {c['mpn'][:24]:<24} {life_str(c):>9} {c['life'][:18]:<18} "
                  f"{esr:>8} {rip:>13} {size:>11} {dash(c['spacing']):>5} {c['stock']:>7} {c['buy']:>4} "
                  f"${c['total']:>7.2f}  {c['pn']}{' (hybrid)' if c['type'] == 'Hybrid' else ''}")
        for w in warns:
            print(f"  ! {w}")
    print()


def run_bom(dk, mouser, path, mount, merge=False, life_cap=None):
    path = Path(path)
    vendor = "Mouser" if mouser else "DigiKey"
    rows = list(csv.DictReader(path.open(newline="", encoding="utf-8")))
    results_path = path.with_name(f"{path.stem} - {vendor} results.csv")
    cart_path = path.with_name(f"{path.stem} - {vendor} cart.csv")
    header = ["Part", "Need Qty", "Group", "Life Target (h)", "Rank", "Brand", "Preferred", "MPN", "Series",
              "Life @105C (h)",
              "Rated Life", "Max Temp (C)", "ESR/Z (mOhm)", "Ripple HF", "Ripple LF", "Diameter (mm)",
              "Height (mm)", "Lead Spacing (mm)", "Package", f"{vendor} Stock", f"{vendor} PN", "Packaging",
              "Buy Qty", "Unit $", "Total $", "Warnings", "URL"]
    lines = []
    for n, r in enumerate(rows, 1):
        num = lambda k: float(r[k]) if (r.get(k) or "").strip() else None
        ln = {"cap": num("Capacitance (uF)"), "volts": num("Voltage (V)"), "need": int(r["Qty"]),
              "bipolar": (r.get("Type") or "").strip().lower() == "bipolar", "dia": num("Diameter (mm)"),
              "spacing": num("Lead Spacing (mm)"), "max_h": num("Max Height (mm)"), "merged": [],
              "mpn": (r.get("MPN") or "").strip()}
        ln["part"] = f"{fmt(ln['cap'])}uF {fmt(ln['volts'])}V {r.get('Type') or ''}".strip()
        lines.append(ln)
    with ThreadPoolExecutor(DK_WORKERS) as ex:
        jobs = [None if ln["mpn"] else ex.submit(pools, dk, ln["cap"], ln["volts"], ln["need"], ln["bipolar"], mount,
                                                 ln["dia"], ln["spacing"], ln["max_h"], priced=mouser is None)
                for ln in lines]
        for n, (ln, job) in enumerate(zip(lines, jobs), 1):
            ln["pools"] = job.result() if job else {}
            print(f"[{n}/{len(lines)}] {ln['part']}: " + (f"fixed part {ln['mpn']}" if ln["mpn"] else
                  ", ".join(f"{len(p)} {g.lower()}" for g, p in ln["pools"].items())), file=sys.stderr)
    if not life_cap:  # the kit's weakest polar line sets how much life is worth paying for
        reaches = [reach(ln["pools"]["Electrolytic"]) for ln in lines
                   if not ln["bipolar"] and ln["pools"].get("Electrolytic")]
        life_cap = LIFE_FACTOR * min(reaches) if reaches else None
    if life_cap:
        print(f"life cap: {life_cap:,.0f}h", file=sys.stderr)
    if mouser:  # all lines priced together so every Mouser call carries the full 10 part numbers
        mouser.parts([ln["mpn"] for ln in lines if ln["mpn"]])
        jobs = [(ln, g) for ln in lines for g in ln["pools"]]
        priced = price_all(mouser, [(ln["pools"][g], ln["need"]) for ln, g in jobs], life_cap)
        for (ln, g), p in zip(jobs, priced):
            ln.setdefault("priced", {})[g] = p
    for ln in lines:
        ln["result"] = {"Fixed": finish(fixed_part(dk, mouser, ln["mpn"], ln["need"]))} if ln["mpn"] else \
            {g: finish(ln["priced"][g] if mouser else p, life_cap) for g, p in ln["pools"].items()}
    for note in consolidate(lines) if merge else []:
        print("consolidated: " + note, file=sys.stderr)

    out, cart, total = [], [], 0.0
    for ln in lines:
        part, need, result = ln["part"], ln["need"] + sum(m["need"] for m in ln["merged"]), ln["result"]
        for group, r in result.items():
            top, warns, target = r["top"], r["warns"], round(r["target"]) if r["target"] else ""
            if not top:
                out.append([part, need, group, target, "-"] + [""] * 20 + ["; ".join(warns), ""])
            for i, c in enumerate(top, 1):
                rip = lambda x: f"{x[0]:g} A @ {hz(x[1])}" if x else ""
                out.append([part, need, group if c["type"] != "Hybrid" else "Polymer (hybrid)", target, i, c["brand"],
                            "yes" if c["preferred"] else "", c["mpn"], c["series"], round(c["life105"]),
                            c["life"], c["maxtemp"], fmt(c["esr"] * 1000) if c["esr"] else "",
                            rip(c["ripple_hf"]), rip(c["ripple_lf"]), c["dia"], c["height"], c["spacing"],
                            c["package"], c["stock"], c["pn"], c["packaging"], c["buy"], c["unit"],
                            round(c["total"], 2), "; ".join(warns) if i == 1 else "", c["url"]])
        top = next(iter(result.values()))["top"]  # Electrolytic, or Fixed for a BOM MPN
        if top and "into" not in ln:
            extra = f" (+{', '.join(fmt(m['volts']) + 'V' for m in ln['merged'])})" if ln["merged"] else ""
            cart.append([top[0]["pn"], top[0]["buy"], part + extra])
            total += top[0]["total"]
    cart = merge_cart(cart)
    with results_path.open("w", newline="", encoding="utf-8") as f:
        csv.writer(f).writerows([header] + out)
    # shortcut: column names follow each vendor's cart/BOM upload; if the upload page asks, map them by hand
    cart_header = ["Mouser Part Number", "Quantity", "Customer Part Number"] if mouser else \
        ["Digi-Key Part Number", "Quantity", "Customer Reference"]
    with cart_path.open("w", newline="", encoding="utf-8") as f:
        csv.writer(f).writerows([cart_header] + cart)
    print(f"\n{len(cart)}/{len(rows)} lines in cart, ${total:.2f} (electrolytic #1 picks)\n"
          f"wrote {results_path.name}\nwrote {cart_path.name}", file=sys.stderr)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cap", nargs="?", help="capacitance, e.g. 470uF or 0.47")
    ap.add_argument("volts", nargs="?", help="rated voltage, e.g. 25V")
    ap.add_argument("--bom", help="BOM CSV to search line by line")
    ap.add_argument("--qty", type=int, default=1, help="quantity needed (single part)")
    ap.add_argument("--bipolar", action="store_true", help="search bi-polar parts (single part)")
    ap.add_argument("--smd", action="store_true", help="surface mount instead of radial through-hole")
    ap.add_argument("--dia", type=float, help="required can diameter in mm (single part)")
    ap.add_argument("--spacing", type=float, help="required lead spacing in mm (single part)")
    ap.add_argument("--max-height", type=float, help="tallest can that fits, in mm (single part)")
    ap.add_argument("--consolidate", action="store_true",
                    help="fold lines into a higher-voltage line of the same value and footprint (BOM)")
    ap.add_argument("--life-cap", type=float, metavar="HOURS",
                    help="rated life (h @105C) beyond this counts as equal; BOM default: 2x the weakest line's best")
    ap.add_argument("--vendor", choices=["mouser", "digikey"], default="mouser",
                    help="where to price and buy (default mouser); specs always come from DigiKey")
    ap.add_argument("--fresh", action="store_true",
                    help=f"ignore cached results (they're reused for {CACHE_HOURS}h); use before ordering")
    ap.add_argument("--new-bom", metavar="FILE", help="write an empty BOM CSV (header row only) and exit")
    a = ap.parse_args()
    if a.new_bom:
        path = Path(a.new_bom)
        if path.exists():
            sys.exit(f"{path} already exists; not overwriting it")
        with path.open("w", newline="", encoding="utf-8") as f:
            csv.writer(f).writerow(BOM_COLUMNS)
        print(f"wrote {path}")
        return
    mount = "smd" if a.smd else "radial"
    if not a.bom and not (a.cap and a.volts):
        ap.error("give a capacitance and voltage, or --bom FILE")
    dk = DigiKey(a.fresh)
    mouser = Mouser(a.fresh) if a.vendor == "mouser" else None
    if a.bom:
        run_bom(dk, mouser, a.bom, mount, a.consolidate, a.life_cap)
    else:
        cap, volts = (float(re.match(r"[\d.]+", s).group()) for s in (a.cap, a.volts))
        result = lookup(dk, mouser, cap, volts, a.qty, a.bipolar, mount, a.dia, a.spacing, a.max_height, a.life_cap)
        print_result(f"{fmt(cap)}uF {fmt(volts)}V {'bi-polar ' if a.bipolar else ''}{mount}, qty {a.qty}, "
                     f"priced at {a.vendor}", result)
    print(f"API calls: DigiKey {dk.calls} (+{dk.hits} cached; limit 1000/day)" +
          (f", Mouser {mouser.calls} (+{mouser.hits} cached; limit 1000/day, 30/min)" if mouser else ""),
          file=sys.stderr)


if __name__ == "__main__":
    main()
