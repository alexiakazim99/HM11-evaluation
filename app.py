from flask import Flask, request, render_template_string
from openai import OpenAI
from dotenv import load_dotenv
import os

load_dotenv()

app = Flask(__name__)
client = OpenAI(api_key=os.environ.get("OPENAI_API_KEY"))

PAGE = """
<!doctype html>
<title>Min chattbot</title>
<h1>Min chattbot</h1>
<form method="post">
  <input name="fraga" placeholder="Skriv en fråga" size="50">
  <button>Fråga</button>
</form>
{% if svar %}<p><b>Svar:</b> {{ svar }}</p>{% endif %}
"""

@app.route("/", methods=["GET", "POST"])
def home():
    svar = None
    if request.method == "POST":
        fraga = request.form["fraga"]
        resultat = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": fraga}],
        )
        svar = resultat.choices[0].message.content
    return render_template_string(PAGE, svar=svar)

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5001)