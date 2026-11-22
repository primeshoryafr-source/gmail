import html
from datetime import datetime, timezone

def _fmt(dt):
    return dt.astimezone(timezone.utc).strftime("%d %b %Y, %H:%M UTC")


async def build_transcript(channel, ticket):
    """Builds an HTML transcript of every message in the channel.
    Returns (bytes, filename, message_count)."""
    rows = []
    count = 0
    async for m in channel.history(limit=None, oldest_first=True):
        count += 1
        attachments = "".join(
            f'<div class="att">📎 <a href="{html.escape(a.url)}">{html.escape(a.filename)}</a></div>'
            for a in m.attachments
        )
        embeds = f'<div class="att">[{len(m.embeds)} embed]</div>' if m.embeds else ""
        bot_tag = '<span class="bot">BOT</span>' if m.author.bot else ""
        content = html.escape(m.clean_content).replace("\n", "<br>")
        rows.append(
            f"""
      <div class="msg">
        <img class="av" src="{html.escape(m.author.display_avatar.url)}">
        <div>
          <span class="name">{html.escape(str(m.author))}</span>{bot_tag}
          <span class="time">{_fmt(m.created_at)}</span>
          <div class="content">{content}</div>
          {attachments}{embeds}
        </div>
      </div>"""
        )

    number = f"{ticket['number']:04d}"
    doc = f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<title>Ticket #{number} Transcript</title>
<style>
  body{{background:#313338;color:#dbdee1;font-family:Arial,sans-serif;margin:0;padding:20px}}
  h1{{color:#fff;font-size:20px}} .meta{{color:#949ba4;margin-bottom:20px;font-size:14px}}
  .msg{{display:flex;gap:12px;margin:14px 0}}
  .av{{width:40px;height:40px;border-radius:50%}}
  .name{{color:#fff;font-weight:bold}} .time{{color:#949ba4;font-size:12px;margin-left:8px}}
  .bot{{background:#5865f2;color:#fff;font-size:10px;padding:1px 4px;border-radius:3px;margin-left:6px}}
  .content{{margin-top:2px;line-height:1.4}} .att{{margin-top:4px;font-size:13px}}
  a{{color:#00a8fc}}
</style></head><body>
<h1>🎫 Ticket #{number} — {html.escape(ticket['subject'])}</h1>
<div class="meta">Server: {html.escape(channel.guild.name)} • Messages: {count} • Generated: {_fmt(datetime.now(timezone.utc))}</div>
{''.join(rows)}
</body></html>"""

    return doc.encode("utf-8"), f"transcript-ticket-{number}.html", count
