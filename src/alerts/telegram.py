import requests

def send_telegram(token: str, chat_id: int, text:str):
    url=f"https://api.telegram.org/bot{token}/sendMessage"
    r=requests.post(url,json={"chat_id": chat_id, "text": text})
    r.raise_for_status()
    return r.json()