            "- Natural, conversational tone like a friendly Maldivian\n"
            "- Max 3-4 sentences\n"
            "- NEVER repeat the same news you already mentioned in this conversation\n"
            "- If asked for more — give DIFFERENT stories\n"
            "- Mention @samugacommunity when relevant\n"
            "- Never write in English or Latin script\n"
            "- Never say you cannot search or lack real-time info"
        )

        # Build contents array with history for multi-turn
        contents = []
        if conversation_history:
            for turn in conversation_history[-6:]:
                role = "user" if turn["role"] == "user" else "model"
                contents.append({"role": role, "parts": [{"text": turn["content"]}]})
        contents.append({"role": "user", "parts": [{"text": user_message}]})

        payload = {
            "system_instruction": {"parts": [{"text": system_prompt}]},
            "contents": contents,
            "generationConfig": {"maxOutputTokens": 400, "temperature": 0.7}
        }

        resp = requests.post(url, json=payload, timeout=15)
        if resp.status_code == 200:
            reply = resp.json()["candidates"][0]["content"]["parts"][0]["text"].strip()
            log.info("✅ Gemini Dhivehi chat reply done")
            return reply
        else:
            log.error(f"Gemini Dhivehi chat HTTP {resp.status_code}: {resp.text[:200]}")
    except Exception as e:
        log.error(f"Gemini Dhivehi chat error: {e}")
    return None

def chat_with_claude(user_message, user_id=None):
    try:
        headlines=[]
        try: headlines=get_local_headlines()
        except: pass
        headlines_text="\n".join(headlines[:8]) if headlines else "No recent headlines."

        memory_text=""
        if recent_posts:
            memory_text="Recently posted:\n"+"".join([f"• [{p['cat']}] {p['title']}\n" for p in recent_posts[-5:]])

        web_context=""
        try:
            if needs_web_search(user_message):
                q=user_message
                if any(w in user_message.lower() for w in ["world cup","match","score","won","win"]):
                    q=f"{user_message} 2026 latest"
                web_context=tavily_search(q)
        except: pass

        context=f"LATEST NEWS:\n{headlines_text}"
        if memory_text: context+=f"\n\n{memory_text}"
        if web_context: context+=f"\n\nWEB SEARCH:\n{web_context[:600]}"

        system=f"""You are Samuga AI — smart friendly Maldivian news assistant for Samuga Media.

ABOUT SAMUGA:
Samuga Media delivers trusted Maldivian news. @samugacommunity is our Telegram channel.
Founder & MD: Abdul Muhsin (Manchii/Mutte) — Maldivian entrepreneur
Co-Founder & Editor: Mariyam Ulya (Uly) — journalist & editorial lead

CONTEXT:
{context}

PERSONALITY:
- Warm, friendly, like a knowledgeable Maldivian friend
- Max 4 sentences per reply
- Use context for accurate answers
- Guide to @samugacommunity for more
- If user writes Dhivehi — reply in Dhivehi
- Never say you lack real-time data"""

        messages=get_conversation(user_id).copy() if user_id else []
        messages.append({"role":"user","content":user_message})

        msg=ai.messages.create(model="claude-haiku-4-5-20251001",max_tokens=600,system=system,messages=messages)
        reply=msg.content[0].text.strip()

        if user_id:
            add_to_conversation(user_id,"user",user_message)
            add_to_conversation(user_id,"assistant",reply)
        return reply
    except Exception as e:
        log.error(f"Chat: {e}")
        return "Hey! Something went wrong 😅 Check @samugacommunity for the latest!"

# ── Chat Handler ──────────────────────────────────────────────────────────────
def handle_updates():
    offset=0; bot_mention=f"@{BOT_USERNAME}".lower()
    log.info(f"💬 Chat listening for @{BOT_USERNAME}...")
    while True:
        try:
            resp=requests.get(f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates",
                params={"offset":offset,"timeout":30},timeout=40)
            if resp.status_code!=200: time.sleep(5); continue
            for update in resp.json().get("result",[]):
                offset=update["update_id"]+1
                msg=update.get("message",{})
                if not msg: continue
                text=msg.get("text","")
                if not text: continue
                chat_id=msg["chat"]["id"]
                msg_id=msg["message_id"]
                chat_type=msg["chat"]["type"]
                user_name=msg.get("from",{}).get("first_name","there")
                user_id=str(msg.get("from",{}).get("id",""))

                if chat_type=="private":
                    if text.startswith("/start"):
                        send_text(chat_id,
                            f"👋 Hey {user_name}! I'm <b>Samuga AI</b> — your Maldives news assistant!\n\n"
                            f"Ask me anything about Maldives news, politics, tourism, football or world news.\n\n"
                            f"ދިވެހިން ވެސް ވާހަކަ ދެއްކިދާނެ! 🇲🇻\n\n"
                            f"📡 Follow <b>@samugacommunity</b> for live news updates!",reply_to=msg_id)
                    elif text.startswith("/search "):
                        query=text[8:].strip()
                        log.info(f"🔍 Search: {query}")
                        results=tavily_search(f"{query} maldives")
                        reply=chat_with_claude(f"Tell me about: {query}. Use this info: {results[:400]}", user_id)
                        send_text(chat_id, reply, reply_to=msg_id)
                    else:
                        log.info(f"💬 DM {user_name}: {text[:50]}")
                        # Route Dhivehi to Gemini
                        if is_dhivehi(text):
                            log.info("🇲🇻 Dhivehi detected — using Gemini")
                            headlines = get_local_headlines()
                            context = "\n".join(headlines[:5]) if headlines else ""
                            history = get_conversation(user_id)
                            reply = chat_with_gemini_dhivehi(text, context, history)
                            if reply:
                                add_to_conversation(user_id, "user", text)
                                add_to_conversation(user_id, "assistant", reply)
                            else:
                                reply = chat_with_claude(text, user_id)
                        else:
                            reply = chat_with_claude(text, user_id)
                        send_text(chat_id, reply, reply_to=msg_id)

                elif chat_type in ["group","supergroup"]:
                    if bot_mention in text.lower():
                        clean=text.lower().replace(bot_mention,"").strip()
                        if clean:
                            log.info(f"💬 Group {user_name}: {clean[:50]}")
                            if is_dhivehi(clean):
                                log.info("🇲🇻 Dhivehi group mention — using Gemini")
                                headlines = get_local_headlines()
                                context = "\n".join(headlines[:5]) if headlines else ""
                                history = get_conversation(user_id)
                                reply = chat_with_gemini_dhivehi(clean, context, history)
                                if reply:
                                    add_to_conversation(user_id, "user", clean)
                                    add_to_conversation(user_id, "assistant", reply)
                                else:
                                    reply = chat_with_claude(clean, user_id)
                            else:
                                reply = chat_with_claude(clean, user_id)
                            send_text(chat_id, reply, reply_to=msg_id)
        except Exception as e:
            log.error(f"Update loop: {e}"); time.sleep(5)

# ── Entry ─────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    log.info("🚀 Samuga News Bot v3.1 starting...")
    log.info("📅 7AM-6PM: every 30min | Night: social only")
    log.info("🌅 7AM Morning Brief | 🌙 12AM Night Summary | 📊 Friday Weekly Digest")
    log.info("💬 Smart chat with history, Tavily search, Dhivehi support")

    seen_on_start=load_seen()
    log.info(f"📚 Loaded {len(seen_on_start)} seen articles")

    threading.Thread(target=handle_updates, daemon=True).start()

    scheduler=BlockingScheduler(timezone="UTC")
    scheduler.add_job(scheduled_check, "interval", minutes=30)
    # Morning brief 7AM MVT = 2AM UTC
    scheduler.add_job(send_morning_brief, "cron", hour=2, minute=0)
    # Night summary 12AM MVT = 7PM UTC (previous day)
    scheduler.add_job(send_night_summary, "cron", hour=19, minute=0)
    # Weekly digest Friday 6PM MVT = 1PM UTC Friday
    scheduler.add_job(send_weekly_digest, "cron", day_of_week="fri", hour=13, minute=0)

    log.info("⏰ Scheduler started!")
    scheduler.start()
