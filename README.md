# SlideCraft Studio — Complete Local Project

A Flask-based presentation studio with a polished landing page, sign-up/login, a personal workspace, detailed AI slide generation, PPTX analysis, text-based comparison, basic text cleanup, and PDF, PowerPoint, and Word export.

## Requirements
- Windows 10/11
- Python 3.11+ (Python 3.13 is supported)
- Internet only if you want Gemini or Groq generation

## Install on Windows / PowerShell

1. Extract the ZIP completely.
2. Open the extracted `SlideCraft_Studio_Complete` folder in VS Code.
3. Open Terminal → New Terminal. Confirm that `app.py` and `requirements.txt` are visible in Explorer.
4. The ZIP already includes a `.env` file. Open it in VS Code and configure the optional keys for Gemini and OAuth. Do not run `Copy-Item .env.example .env` unless you deleted `.env`.
5. Run:

```powershell
py -3.13 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe app.py
```

If you already created `.venv`, skip the first command. Keep the existing `.env` file and its credentials intact.

6. Open http://127.0.0.1:5000/
7. Choose **Start creating**, make an account, sign in with Google/Facebook, or continue as a guest.

## Google OAuth setup
1. Create a Google Cloud project and enable the Google Identity / OAuth 2.0 client.
2. Add an OAuth client ID and secret in `.env`:
   - `GOOGLE_CLIENT_ID`
   - `GOOGLE_CLIENT_SECRET`
3. Add authorised redirect URI:
   - http://127.0.0.1:5000/auth/google/callback
4. If credentials are missing, the Google button remains visible but shows a setup message instead of pretending the provider is ready.

## Facebook OAuth setup
1. Create a Meta/Facebook app and enable Facebook Login.
2. Add the app credentials in `.env`:
   - `FACEBOOK_CLIENT_ID`
   - `FACEBOOK_CLIENT_SECRET`
3. Add the valid callback URL in the app configuration:
   - http://127.0.0.1:5000/auth/facebook/callback
4. The app uses the Facebook Graph API to read the account name, email, and profile image when available.

## AI slide generation (optional)
Open `.env` and set `GEMINI_API_KEY` and/or `GROQ_API_KEY` to your provider keys. `GEMINI_MODEL` and `GROQ_MODEL` keep their configured/default model values. Restart the server after changing keys and keep `.env` private. Gemini is tried first; Groq is used if Gemini fails or takes too long. Both providers receive the same prompt and must return a slide explanation, substantive key points, and presenter notes for every slide. If configured providers cannot produce complete content, generation reports an error instead of replacing it with an outline. Without either key, the creator clearly labels its local result as a starter outline.

## Unsplash slide photography (optional)
Set `UNSPLASH_ACCESS_KEY` in the existing `.env` file to enable topic-specific photo search. Create an API application and copy its access key from your Unsplash developer account. The key is used only in server-side API request headers; never paste it into browser code. SlideCraft searches landscape photos through the official Unsplash API, displays the CDN URL returned by Unsplash in the editor, triggers the returned download endpoint when using a photo in a deck, and embeds the selected image bytes in the PowerPoint. Slide credits link both the photographer profile and Unsplash with referral parameters. Photo selections and downloaded image bytes are cached under `.cache/unsplash/`; slides use native editable PowerPoint visuals when the key is missing or a photo is unavailable.

## Adobe Firefly image generation (optional)
Create an Adobe Firefly API integration in Adobe Developer Console, then add the server-side credentials to the existing `.env` file:

- `ADOBE_FIREFLY_CLIENT_ID`
- `ADOBE_FIREFLY_CLIENT_SECRET`
- `ADOBE_FIREFLY_SCOPES` (default: `openid,AdobeID,firefly_api,ff_apis`)

Restart Flask after configuring credentials. Firefly uses Adobe's OAuth server-to-server token flow and the documented asynchronous image-generation and job-status endpoints. Prompts, access tokens, and API keys stay on the server. Generated images are cached under `.cache/firefly/` and are only added to the deck after you accept a preview. Without credentials, the editor explains that Firefly is unavailable; existing Unsplash and native visual workflows remain usable.

## Features
- Responsive premium landing page with blue gradient / presentation preview design
- Sign-up and sign-in with Werkzeug password hashing and server-side normalized email validation
- SQLite accounts and saved generated-project records
- Detailed AI generation with Gemini and Groq; explicitly labeled offline starter outline if no key is configured
- Topic-matched Unsplash photography with local caching, linked photographer credits, and native visual fallbacks
- Recommendations and pre-generation selection for the existing editable slide layouts
- Optional Adobe Firefly image previews with accept-before-use workflow
- Lightweight liquid-glass cursor effect for fine pointers; disabled for touch and reduced-motion preferences
- Content-aware cover, image/text, comparison, diagram, and data layouts
- Editable slide explanations, key points, visual direction, diagrams, bar/line/pie/doughnut/comparison charts, and speaker notes
- PPTX export with embedded images and editable PowerPoint-native shapes and charts; structural checks report geometry concerns but do not replace rendering in PowerPoint
- Editable Word (.docx) export with slide headings, bullets, notes, chart/diagram details, and available attributions
- PDF export with widescreen, print-ready, A4, US Letter, compressed, speaker-notes, and 2/4/6-slide handout layouts
- PPTX text extraction and heuristic review
- Two-file text-based comparison
- Basic text cleanup / optimization
- Flash messages and protected workspace routes

## Important limitations
- The score is a heuristic based on extracted text; it does not visually inspect slide layouts or verify facts.
- The optimizer cleans extracted text; it does not preserve original slide styling when exporting.
- Signup checks email syntax and prevents case-insensitive duplicates, but does not verify inbox ownership. No email delivery provider is configured.
- This is a local student project, not production-ready public hosting. For deployment, add CSRF protection, secure cookie configuration, rate limiting, account recovery, HTTPS, and a production server.
- Never upload sensitive or confidential presentations unless you have permission.

## Troubleshooting
- `requirements.txt not found`: VS Code is opened in the wrong folder. Use `cd` to enter the folder containing `app.py`.
- `app.py not found`: re-extract the ZIP; do not open the ZIP as if it were a folder.
- PowerShell shows `>>`: press `Ctrl+C` once to exit continuation mode, then enter one command at a time.
- `Address already in use`: stop the other Flask server with `Ctrl+C` or change `PORT` in `.env`.


## Where to put your API key
The `.env` file is in the same folder as `app.py`. Open `.env` and edit this line:
`GEMINI_API_KEY=PASTE_YOUR_KEY_HERE`
Save the file and restart the Flask server. Obtain your own key from Google AI Studio. Never share or commit `.env`.
