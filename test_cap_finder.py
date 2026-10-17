"""Offline checks for cap_finder.py parsing and buying logic: python3 test_cap_finder.py"""
import tempfile
from pathlib import Path
import cap_finder
cap_finder.CACHE = Path(tempfile.mkdtemp())   # never touch the real cache
from cap_finder import (amps, best_buy, candidates, consolidate, life105, max_temp, mm, mouser_variation,
                          ohms, price_all, price_mouser, rank, warnings, fixed_part, merge_cart)

assert mm('0.394" Dia (10.00mm)') == 10.0 and mm("-") is None
assert ohms("39 mOhms") == 0.039 and ohms("15mOhm") == 0.015 and ohms("1.2 Ohms") == 1.2 and ohms("-") is None
assert amps("731.5 mA @ 120 Hz") == (0.7315, 120) and amps("1.33 A @ 100 kHz") == (1.33, 100000)
assert max_temp("-40°C ~ 105°C") == 105
assert life105("2000 Hrs @ 105°C", False) == 2000
assert life105("2000 Hrs @ 125°C", False) == 8000      # electrolytic: 2x per 10C
assert life105("2000 Hrs @ 125°C", True) == 20000      # solid polymer: 10x per 20C
assert life105("-", False) == 0

ct = {"MarketPlace": False, "DigiKeyProductNumber": "X-CT", "PackageType": {"Name": "Cut Tape"},
      "MinimumOrderQuantity": 1, "QuantityAvailableforPackageType": 1000,
      "StandardPricing": [{"BreakQuantity": 1, "UnitPrice": 0.63}, {"BreakQuantity": 10, "UnitPrice": 0.395}]}
assert best_buy([ct], 9)["buy"] == 10        # 10 x 0.395 = 3.95 beats 9 x 0.63 = 5.67
assert best_buy([ct], 3)["buy"] == 3         # 3 x 0.63 = 1.89 beats 3.95
assert best_buy([{**ct, "MarketPlace": True}], 3) is None
assert best_buy([{**ct, "MinimumOrderQuantity": 500}], 1) is None   # reel-only

def c(brand, pref, life, total, series, esr=None, height=12):
    return {"brand": brand, "brand_id": 493 if brand == "Nichicon" else 0, "preferred": pref, "life105": life,
            "total": total, "series": series, "esr": esr, "dia": 10, "height": height, "spacing": 5}
top, _ = rank([c("NoName", False, 9000, 1, "A"), c("Nichicon", True, 5000, 2, "PW"),
            c("Nichicon", True, 5000, 1, "PW"), c("Rubycon", True, 4000, 1, "ZLH")])
assert [(t["brand"], t["total"]) for t in top] == [("Nichicon", 1), ("Rubycon", 1), ("NoName", 1)]

# max height filter on a minimal DigiKey product record
par = lambda **kw: [{"ParameterText": k, "ValueText": v} for k, v in kw.items()]
prod = {"Manufacturer": {"Name": "Rubycon", "Id": 1189}, "ManufacturerProductNumber": "16YXJ47M5X11",
        "QuantityAvailable": 1000, "ProductVariations": [ct],
        "Parameters": par(**{"Operating Temperature": "-40°C ~ 105°C", "Polarization": "Polar",
                             "Package / Case": "Radial, Can", "Size / Dimension": '0.197" Dia (5.00mm)',
                             "Lead Spacing": '0.079" (2.00mm)', "Height - Seated (Max)": '0.492" (12.50mm)',
                             "Lifetime @ Temp.": "2000 Hrs @ 105°C"})}
assert len(candidates([prod], 1, False, "radial", None, None, 12.5)) == 1
assert len(candidates([prod], 1, False, "radial", None, None, 11)) == 0

# consolidation: 47uF 16V and 25V picks share a 5mm can, 50V pick is 6.3mm
def line(volts, need, dia, h=12.5):
    pick = {"dia": dia, "spacing": 2.0 if dia == 5 else 2.5, "height": h, "stock": 1000, "variations": [ct],
            "mpn": f"P{volts}", "esr": None, "ripple_hf": None, "ripple_lf": None, "total": 1, "buy": need,
            "need": need}
    return {"cap": 47, "volts": volts, "need": need, "bipolar": False, "max_h": None, "merged": [],
            "part": f"47uF {volts}V", "result": {"Electrolytic": {"top": [pick], "warns": [], "pool": [pick], "target": None}}}
l16, l25, l50 = line(16, 3, 5), line(25, 2, 5), line(50, 1, 6.3)
assert consolidate([l16, l25, l50]) == ["47uF 16V -> 47uF 25V (5mm can)"]
assert l16["into"] is l25 and "into" not in l50 and l25["result"]["Electrolytic"]["top"][0]["need"] == 5
l16, l25 = line(16, 3, 5), line(25, 2, 5, h=14)
l16["max_h"] = 13
assert consolidate([l16, l25]) == []   # 25V can too tall for the 16V spot
# life cap: past the target, Nichicon first, then lowest ESR, with a 1.5x price cap
pool = [c("Rubycon", True, 20000, 3.0, "YXF", 0.09), c("Nichicon", True, 8000, 1.0, "HE", 0.06),
        c("Chemi-Con", True, 10000, 0.9, "KZE", 0.03), c("Panasonic", True, 12000, 1.2, "FR", 0.02)]
assert [x["series"] for x in rank(pool)[0]][:1] == ["YXF"]               # no cap: longest life wins
top, target = rank(pool, life_cap=8000)
assert target == 8000 and [x["series"] for x in top] == ["HE", "FR", "KZE"]  # Nichicon, then ESR; YXF over cap
top, _ = rank(pool[:1] + pool[2:], life_cap=8000)
assert [x["series"] for x in top] == ["FR", "KZE", "YXF"]                 # no Nichicon: lowest ESR first
short = [c("Nichicon", True, 4000, 1, "A"), c("Rubycon", True, 5000, 1, "B")]
assert rank(short, life_cap=8000) == ([short[1], short[0]], 5000)        # can't reach cap: longest life
tall = [dict(c("Nichicon", True, 9000, 1, "T", height=21.5), mpn="T", ripple_hf=None, ripple_lf=None, buy=1),
        dict(c("Nichicon", True, 9000, 1, "S", height=17.5), mpn="S", ripple_hf=None, ripple_lf=None, buy=1)]
assert any(w.startswith("taller: 21.5mm vs 17.5mm") for w in warnings(tall, tall))
# Mouser: part -> variation, price breaks, Mult, and pricing a pool with a fake client
mp = {"MouserPartNumber": "647-UHW1E471MPD", "ManufacturerPartNumber": "UHW1E471MPD", "Min": "1", "Mult": "1",
      "AvailabilityInStock": "3034", "ProductDetailUrl": "https://mouser/x",
      "ProductAttributes": [{"AttributeName": "Packaging", "AttributeValue": "Bulk"}],
      "PriceBreaks": [{"Quantity": 1, "Price": "$0.65", "Currency": "USD"}, {"Quantity": 10, "Price": "$0.38", "Currency": "USD"}]}
v = mouser_variation(mp)
assert best_buy([v], 1) == {"pn": "647-UHW1E471MPD", "packaging": "Bulk", "buy": 1, "unit": 0.65, "total": 0.65}
assert best_buy([v], 7)["buy"] == 10                      # 10 x 0.38 = 3.80 beats 7 x 0.65 = 4.55
assert best_buy([{**v, "Mult": 5}], 3)["buy"] == 10       # must buy in multiples of 5; 5 isn't a break, 10 is cheaper
assert best_buy([{**v, "QuantityAvailableforPackageType": 0}], 1) is None   # out of stock

class FakeMouser:
    def __init__(self): self.asked = []
    def parts(self, mpns):
        self.asked += mpns
        return {"UHW1E471MPD": [mp]}
pool = [dict(c("Nichicon", True, 10000, 0, "HW"), mpn="UHW1E471MPD", need=1, pn="", packaging="", buy=1, unit=0),
        dict(c("Nichicon", True, 7000, 0, "HE"), mpn="UHE1E471MPD6TD", need=1, pn="", packaging="", buy=1, unit=0)]
fm = FakeMouser()
priced = price_mouser(fm, pool, 1)
assert fm.asked == ["UHW1E471MPD", "UHE1E471MPD6TD"]
assert [(x["mpn"], x["pn"], x["total"], x["stock"], x["url"]) for x in priced] == \
    [("UHW1E471MPD", "647-UHW1E471MPD", 0.65, 3034, "https://mouser/x")]  # HE not sold at Mouser: dropped
# Mouser cache: a part number is only ever sent once, misses included
import cap_finder
class FakeHTTP:
    calls = []
    def __call__(self, req):
        body = __import__("json").loads(req.data)["SearchByPartRequest"]["mouserPartNumber"].split("|")
        FakeHTTP.calls.append(body)
        hits = [p for p in [mp] if p["ManufacturerPartNumber"] in body]
        return __import__("io").BytesIO(__import__("json").dumps({"Errors": [], "SearchResults": {"Parts": hits}}).encode())
m = cap_finder.Mouser.__new__(cap_finder.Mouser)
m.key, m.calls, m.hits, m.last, m.cache, m.fresh = "k", 0, 0, 0.0, {}, False
real, cap_finder.urllib.request.urlopen = cap_finder.urllib.request.urlopen, FakeHTTP()
cap_finder.MOUSER_GAP = 0
try:
    many = [f"P{i:02d}" for i in range(13)] + ["UHW1E471MPD"]
    r = m.parts(many)
    assert [len(c) for c in FakeHTTP.calls] == [10, 4] and r["UHW1E471MPD"] == [mp] and r["P00"] == []
    m.parts(["P00", "UHW1E471MPD", "P99"])
    assert FakeHTTP.calls[-1] == ["P99"] and m.calls == 3     # cached ones not re-sent
finally:
    cap_finder.urllib.request.urlopen = real
# DigiKey fetch: an empty result has no ProductsCount; >300 matches split by manufacturer
dk = cap_finder.DigiKey.__new__(cap_finder.DigiKey)
replies = [{"Products": [], "SearchLocaleUsed": {}}]
dk.search = lambda body: replies.pop(0)
assert dk.fetch("69", 10, 16, "radial") == []
sent = []
def fake(body):
    sent.append(body)
    f = body["FilterOptionsRequest"]
    if "ManufacturerFilter" not in f:
        return {"Products": [1] * 50, "ProductsCount": 476, "FilterOptions": {"Manufacturers": [
            {"Id": 493, "ProductCount": 200}, {"Id": 10, "ProductCount": 150}, {"Id": 565, "ProductCount": 126}]}}
    n = sum({493: 200, 10: 150, 565: 126}[m["Id"]] for m in f["ManufacturerFilter"])
    return {"Products": [1] * min(50, n - body["Offset"]), "ProductsCount": n}
dk.search = fake
assert len(dk.fetch("58", 470, 25, "radial")) == 476
assert [[m["Id"] for m in b["FilterOptionsRequest"]["ManufacturerFilter"]] for b in sent[1:] if b["Offset"] == 0] \
    == [[493], [10, 565]]                                   # 200 | 150+126, each <= 300
assert all(b["FilterOptionsRequest"]["SearchOptions"] == ["InStock"]
           and b["FilterOptionsRequest"]["MarketPlaceFilter"] == "ExcludeMarketPlace" for b in sent)
# Mouser answers persist on disk: a new run (new Mouser object) sends nothing for known part numbers
m2 = cap_finder.Mouser.__new__(cap_finder.Mouser)
m2.key, m2.calls, m2.hits, m2.last, m2.cache, m2.fresh = "k", 0, 0, 0.0, {}, False
cap_finder.urllib.request.urlopen, sent_before = FakeHTTP(), len(FakeHTTP.calls)
try:
    assert m2.parts(["UHW1E471MPD", "P00"]) == {"UHW1E471MPD": [mp], "P00": []}
    assert len(FakeHTTP.calls) == sent_before and m2.calls == 0 and m2.hits == 2
    m2.cache, m2.fresh = {}, True                              # --fresh: ignore the disk cache
    m2.parts(["UHW1E471MPD"])
    assert m2.calls == 1
finally:
    cap_finder.urllib.request.urlopen = real

# DigiKey search results persist on disk too
dk = cap_finder.DigiKey.__new__(cap_finder.DigiKey)
dk.fresh, dk.hits, dk.lock = False, 0, __import__("threading").Lock()
reqs = []
dk._request = lambda body: reqs.append(body) or {"Products": [], "ProductsCount": 0}
body = {"Keywords": "", "Limit": 50, "Offset": 0}
assert dk.search(body) == dk.search(dict(reversed(list(body.items())))) == {"Products": [], "ProductsCount": 0}
assert len(reqs) == 1 and dk.hits == 1                         # same request (any key order) served from disk

# price_all: top 5 per job in one batch; only a job with nothing in stock widens by 10
class CountingMouser:
    def __init__(self, stocked): self.stocked, self.sent, self.seen = stocked, [], set()
    def parts(self, mpns):                                     # like Mouser: only unseen part numbers go out
        new = [x for x in dict.fromkeys(mpns) if x not in self.seen]
        self.seen.update(new)
        if new: self.sent.append(new)
        return {cap_finder.norm(x): ([dict(mp, ManufacturerPartNumber=x)] if x in self.stocked else []) for x in mpns}
def job(prefix, k):
    return [dict(c("Nichicon", True, 10000 - i, 0, f"{prefix}{i}"), mpn=f"{prefix}{i}", need=1, pn="", packaging="",
                 buy=1, unit=0) for i in range(k)], 1
cm = CountingMouser({"A0", "B7"})                              # A's #1 is stocked; B's first hit is its 8th part
priced = price_all(cm, [job("A", 20), job("B", 20)])
assert [x["mpn"] for x in priced[0]] == ["A0"] and [x["mpn"] for x in priced[1]] == ["B7"]
assert [len(s) for s in cm.sent] == [10, 10]                   # A0-4 + B0-4 together, then only B5-14
# bi-polar parts may be 85C; polar parts must be 105C
bp = dict(prod, Parameters=par(**{"Operating Temperature": "-40°C ~ 85°C", "Polarization": "Bi-Polar",
                                  "Package / Case": "Radial, Can", "Size / Dimension": '0.197" Dia (5.00mm)'}))
assert len(candidates([bp], 1, True, "radial", None, None)) == 1
assert len(candidates([dict(bp, Parameters=par(**{"Operating Temperature": "-40°C ~ 85°C", "Polarization": "Polar",
                                                   "Package / Case": "Radial, Can"}))], 1, False, "radial", None, None)) == 0

# a BOM MPN is bought as-is at Mouser; [] when Mouser has none in stock
wima = dict(mp, ManufacturerPartNumber="MKS2D034701E00KSSD", MouserPartNumber="505-MKS2.47/100/10", Manufacturer="WIMA")
class OneMouser:
    def parts(self, mpns): return {cap_finder.norm(x): ([wima] if x == "MKS2D034701E00KSSD" else []) for x in mpns}
(f,) = fixed_part(None, OneMouser(), "MKS2D034701E00KSSD", 2)
assert (f["brand"], f["pn"], f["buy"], f["mpn"]) == ("WIMA", "505-MKS2.47/100/10", 2, "MKS2D034701E00KSSD")
assert fixed_part(None, OneMouser(), "MKS2F034701M00KSSD", 1) == []
# two BOM lines buying the same part become one cart row
assert merge_cart([["505-A", 1, "0.47uF 50V"], ["710-B", 4, "10uF"], ["505-A", 1, "0.47uF 100V"]]) == \
    [["505-A", 2, "0.47uF 50V + 0.47uF 100V"], ["710-B", 4, "10uF"]]
print("ok")
