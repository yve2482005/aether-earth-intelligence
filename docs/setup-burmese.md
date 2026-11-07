# AETHER Earth Intelligence — Setup လမ်းညွှန် (မြန်မာဘာသာ)

ဒီလမ်းညွှန်က programming မတတ်တဲ့သူအတွက်ပါ။ အဆင့်လိုက် လုပ်သွားပါ။
တစ်ခုခု မအောင်မြင်ရင် အဆင့် ၁၂ (ပြဿနာဖြေရှင်းနည်း) ကို ကြည့်ပါ။
အောက်က output နမူနာတွေက ဥပမာပါ — တကယ့် run ရလဒ်နဲ့ နံပါတ် ကွာနိုင်ပါတယ်။

## အဆင့် ၁ — GitHub repository ဖွင့်နည်း

1. https://github.com ကို ဖွင့်ပြီး sign in လုပ်ပါ။
2. ညာဘက်အပေါ်က `+` → **New repository** ကို နှိပ်ပါ။
3. Repository name: `aether-earth-intelligence` လို့ ရိုက်ပါ။
4. **Public** ကို ရွေးပါ။ **Add a README file** ကို အမှန်ခြစ်မထားပါနဲ့။
5. **Create repository** ကို နှိပ်ပါ။
6. ပေါ်လာတဲ့ စာမျက်နှာက URL ကို မှတ်ထားပါ — ဥပမာ
   `https://github.com/သင့်နာမည်/aether-earth-intelligence.git`

## အဆင့် ၂ — Project files ထည့်နည်း

1. ကွန်ပျူတာမှာ terminal (Windows ဆို Git Bash) ဖွင့်ပါ။
2. အောက်က command တွေကို ရိုက်ပါ (သင့်နာမည်နဲ့ အစားထိုးပါ) —
   ဥပမာ command တွေပါ:

```bash
git clone https://github.com/သင့်နာမည်/aether-earth-intelligence.git
cd aether-earth-intelligence
```

3. ဒီ project ရဲ့ file တွေကို ဒီ folder ထဲ ကူးထည့်ပါ။
   အဓိက folder တွေက: `backend/`, `scripts/`, `tests/`, `docs/`,
   `config/`, `.github/workflows/`, `main.py`, `requirements.txt`,
   `.env.example`, `.gitignore`
4. ပြီးရင် push လုပ်ပါ:

```bash
git add .
git commit -m "AETHER initial project"
git push origin main
```

5. အောင်မြင်ရင် GitHub စာမျက်နှာမှာ file တွေ ပေါ်လာပါမယ်။

## အဆင့် ၃ — Google Sheet ဖွင့်နည်း

1. https://sheets.google.com ကို ဖွင့်ပါ။
2. **Blank spreadsheet** ကို နှိပ်ပါ။
3. နာမည် ပေးပါ — ဥပမာ `AETHER_Mirror`
4. URL ထဲက ID ကို မှတ်ထားပါ — ဥပမာ
   `https://docs.google.com/spreadsheets/d/1AbC...XyZ/edit`
   ဆိုရင် ID က `1AbC...XyZ` ဖြစ်ပါတယ်။ ဒါကို အဆင့် ၇ မှာ သုံးပါမယ်။

## အဆင့် ၄ — Google Cloud project နဲ့ service account လုပ်နည်း

1. https://console.cloud.google.com ကို ဖွင့်ပြီး Google account နဲ့ sign in လုပ်ပါ။
2. အပေါ်က project ရွေးတဲ့နေရာမှာ **New Project** → နာမည် `aether` ပေးပြီး
   **Create** ကို နှိပ်ပါ။
3. ဘယ်ဘက် menu → **IAM & Admin → Service Accounts** ကို သွားပါ။
4. **Create Service Account** → နာမည် `aether-sheets` ပေးပြီး **Create**။
5. Role မရွေးပဲ **Continue** → **Done** ကို နှိပ်ပါ။
6. အခု ဖန်တီးထားတဲ့ service account ကို နှိပ်ပါ → **Keys** tab →
   **Add Key → Create new key → JSON** → **Create**။
7. JSON file တစ်ခု download ဖြစ်လာပါမယ် — ဒါက **password နဲ့ တူပါတယ်**။
   ဘယ်သူ့ကိုမှ မပို့ပါနဲ့၊ GitHub မှာ မတင်ပါနဲ့။

## အဆင့် ၅ — Google Sheets API ဖွင့်နည်း

1. Google Cloud Console မှာ **APIs & Services → Library** ကို သွားပါ။
2. `Google Sheets API` လို့ ရှာပါ → နှိပ်ပါ → **Enable** ကို နှိပ်ပါ။
3. `Google Drive API` ကိုလည်း ဒီအတိုင်း ရှာပြီး **Enable** လုပ်ပါ
   (Drive backup သုံးချင်ရင်)။

## အဆင့် ၆ — Spreadsheet ကို service account နဲ့ share နည်း

1. အဆင့် ၄ မှာ ရတဲ့ service account ရဲ့ email ကို ကူးပါ —
   ပုံစံက `aether-sheets@aether-xxxx.iam.gserviceaccount.com` လိုမျိုး ဖြစ်ပါတယ်။
2. အဆင့် ၃ က Google Sheet ကို ဖွင့်ပါ → ညာဘက်အပေါ်က **Share** ကို နှိပ်ပါ။
3. အဲဒီ email ကို ထည့်ပြီး **Editor** အဖြစ် ပေးပါ → **Share** ကို နှိပ်ပါ။
4. ဒါမလုပ်ရင် AETHER က Sheet ထဲ ရေးလို့ မရပါဘူး။

## အဆင့် ၇ — GitHub Actions Secrets ထည့်နည်း

1. GitHub မှာ သင့် repository ကို ဖွင့်ပါ → **Settings** →
   **Secrets and variables → Actions** ကို သွားပါ။
2. **New repository secret** ကို နှိပ်ပါ:
   - Name: `GOOGLE_SHEET_ID`, Value: အဆင့် ၃ က Sheet ID
   - Name: `GOOGLE_SERVICE_ACCOUNT_JSON`, Value: အဆင့် ၄ က JSON file
     ထဲက စာသားအကုန်လုံး (file ကို text editor နဲ့ ဖွင့်ပြီး ကူးပါ)
3. **Add secret** ကို နှိပ်ပါ။ Secret တွေက ဘယ်သူမှ မြင်လို့ မရပါဘူး။

## အဆင့် ၈ — Tests run နည်း

1. Terminal မှာ project folder ထဲ သွားပါ။
2. Python 3.12 နဲ့ dependency တွေ သွင်းပါ:

```bash
pip install -r requirements.txt
python -m pytest tests/ -q
```

3. အောင်မြင်ရင် ဒီလို ပေါ်ပါမယ် (နံပါတ်က ဥပမာပါ):

```
368 passed, 2 skipped
```

4. `failed` ပေါ်ရင် အဆင့် ၁၂ ကို ကြည့်ပါ။ Tests တွေက internet မလိုပါဘူး —
   fixture (အတုဒေတာ) နဲ့ပဲ စစ်ပါတယ်။

## အဆင့် ၉ — Collector ကိုယ်တိုင် run နည်း

```bash
python main.py collect
```

အောင်မြင်ရင် run summary ပေါ်လာပါမယ်။ GDACS disaster feed ကိုလည်း
စမ်းချင်ရင်:

```bash
python main.py gdacs
```

## အဆင့် ၁၀ — Logs ကြည့်နည်း

- Terminal မှာ run လုပ်ရင် log တွေက terminal မှာပဲ ပေါ်ပါတယ်။
- GitHub Actions က run ရင်: repository → **Actions** tab → run တစ်ခုကို
  နှိပ်ပြီး အဆင့်တစ်ခုချင်းစီရဲ့ log ကို ကြည့်နိုင်ပါတယ်။
- အမှားပါရင် အနီရောင် `✕` ပြပါတယ် — အဲဒီ အဆင့်ကို နှိပ်ပြီး
  နောက်ဆုံး စာကြောင်းတွေကို ဖတ်ပါ။

## အဆင့် ၁၁ — Spreadsheet စစ်နည်း

1. အဆင့် ၃ က Sheet ကို ဖွင့်ပါ။
2. Tab တွေကို ကြည့်ပါ: `catalog` tab မှာ dataset တစ်ခုချင်းစီရဲ့
   DB rows, Sheet rows, နောက်ဆုံး sync အချိန်တို့ ပါတယ်။
3. `alerts`, `earthquakes` (သို့မဟုတ် သက်ဆိုင်ရာ tab) တွေမှာ
   ဒေတာ အသစ်တွေ ရောက်မရောက် စစ်ပါ။
4. ဒေတာ မရောက်ရင် အဆင့် ၁၂ ကို ကြည့်ပါ။

## အဆင့် ၁၂ — Source ပျက်ရင် ပြဿနာဖြေရှင်းနည်း

| လက္ခဏာ | စစ်ရမှာ | ဖြေရှင်းနည်း |
|---|---|---|
| Collector က 429 error ပေး | Open-Meteo နေ့စဉ် ကန့်သတ် (10,000 requests/day/IP) ကုန်သွားတာ | မနက်ဖြန်မှ ပြန်စမ်းပါ။ ထပ်ထပ် run မလုပ်ပါနဲ့။ |
| Sheets က `not connected` ပြ | Service account / Sheet share | အဆင့် ၅, ၆, ၇ ကို ပြန်စစ်ပါ။ |
| Tests fail | ဘယ် test လဲ ကြည့်ပါ | Error message ကို မှတ်ပြီး `docs/TROUBLESHOOTING.md` ကို ကြည့်ပါ။ |
| ငလျင်/ဆူနာမီ ဒေတာ မရောက် | USGS / PTWC site ဖွင့်လို့ ရလား | Site ပျက်နေရင် စောင့်ပါ — system က သူ့အလိုလို ပြန်စမ်းပါတယ်။ |
| Disk ပြည့်နေ | `data/` folder ကြီးနေလား | Backup file အဟောင်းတွေ (`.db.gz`) ကို စစ်ပါ — မဖျက်ခင် သေချာစစ်ပါ။ |

အရေးကြီးဆုံးစည်းမျဉ်း: ပျက်နေတဲ့ feed က "ဒေတာ မရှိ" လို့ မဆိုလိုပါဘူး —
ဘေးကင်းတယ်လို့ ဘယ်တော့မှ မယူဆပါနဲ့။ Feed မရရင် stale/unavailable လို့ပဲ
ပြပါတယ်။

## အဆင့် ၁၃ — Credentials ကာကွယ်နည်း

1. `.env` file နဲ့ service account JSON ကို **GitHub မှာ ဘယ်တော့မှ မတင်ပါနဲ့**
   (`.gitignore` မှာ တားထားပြီးသား ဖြစ်ပါတယ်)။
2. Secret တွေကို chat, email, screenshot တွေမှာ မပို့ပါနဲ့။
3. Key ပေါက်ကြားသွားတယ် ထင်ရင် Google Cloud Console မှာ key အဟောင်းကို
   ဖျက်ပြီး အသစ်လုပ်ပါ၊ GitHub Secret ကိုလည်း အသစ်နဲ့ လဲပါ။
4. Android app ထဲမှာ service account / API key **ဘယ်တော့မှ မထည့်ပါနဲ့**။

## အဆင့် ၁၄ — Scheduled collection ဖွင့်နည်း

1. GitHub repository → **Actions** tab → **collector** workflow ကို ဖွင့်ပါ။
2. ဒီ workflow က ၃၀ မိနစ်တခါ အလိုလို run ပါတယ် (`.github/workflows/collector.yml`
   ထဲက schedule ကို ပြင်လို့ရပါတယ်)။
3. ချက်ချင်း run ချင်ရင် **Run workflow** ကို နှိပ်ပါ။
4. GitHub Actions ရဲ့ schedule က best-effort ပါ — မိနစ်အတိအကျ
   run မယ်လို့ အာမမခံပါဘူး။

## အဆင့် ၁၅ — Delivery API ကို Android အတွက် ပြင်နည်း

1. API ကို စမ်းပါ:

```bash
python main.py api
```

   ပြီးရင် browser မှာ `http://127.0.0.1:8000/v1/global/overview` ကို ဖွင့်ပါ —
   JSON data ပေါ်လာရင် API အလုပ်လုပ်နေပါပြီ။
2. Android app က API ကို ဒီအတိုင်း ခေါ်ပါတယ်:
   `GET /v1/global/search`, `GET /v1/global/weather`,
   `GET /v1/global/overview` — အသေးစိတ်က `docs/` ထဲက API doc မှာ ပါတယ်။
3. API က read-only ပါ — app က ဒေတာ ဖတ်ရုံပဲ၊ ပြင်လို့ မရပါဘူး။
4. Production မှာ API ကို HTTPS နောက်မှာ ထားပါ။ API key / service
   account ကို app ထဲ ဘယ်တော့မှ မထည့်ပါနဲ့။
5. အချက်အလက် လတ်ဆတ်မှု (freshness) နဲ့ source ကို app မှာ အမြဲပြပါ —
   ဒေတာဟောင်းကို live လို့ မပြပါနဲ့။
