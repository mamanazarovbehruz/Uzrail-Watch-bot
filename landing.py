import os
from flask import Flask, request, Response

app = Flask(__name__)

@app.get("/")
def health():
    return "UzRail landing is running", 200

@app.get("/go")
def go():
    lang = (request.args.get("lang") or "uz").lower()
    dep = request.args.get("dep") or ""
    arv = request.args.get("arv") or ""
    date = request.args.get("date") or ""

    target = f"https://eticket.railway.uz/{lang}/home"

    html = f"""<!doctype html>
<html>
<head>
  <meta charset="utf-8"/>
  <meta name="viewport" content="width=device-width,initial-scale=1"/>
  <title>Chipta ochish</title>
</head>
<body style="font-family:Arial; background:#0b1220; color:#fff;
             display:flex; justify-content:center; align-items:center; height:100vh">
  <div style="max-width:420px; width:92%; background:#111a2e;
              padding:20px; border-radius:14px">
    <h3>Qayerda ochamiz?</h3>
    <p>📍 <b>{dep} → {arv}</b><br/>📅 <b>{date}</b></p>

    <a href="{target}" style="display:block; margin:12px 0; padding:12px;
       background:#1e88e5; color:#fff; text-decoration:none;
       border-radius:10px; text-align:center">
      🌐 Brauzerda ochish
    </a>

    <a href="{target}" style="display:block; margin:12px 0; padding:12px;
       background:#43a047; color:#fff; text-decoration:none;
       border-radius:10px; text-align:center">
      📱 Uz Rail Ticket ilovasi
    </a>
  </div>
</body>
</html>
"""
    return Response(html, mimetype="text/html")


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "8080")))
