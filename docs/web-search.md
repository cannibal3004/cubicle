# Web search

Every bot can search the web and read pages with built-in tools, through one search provider that you
set up once:

- **`web_search`** finds pages. Results are compact (title, link, date and a few passages), so a search
  does not fill the conversation. The bot can narrow it to the last day, week, month or year, or to
  pages since a date, to sites it trusts or away from others, and to a kind of result (news, and with
  Exa also companies, people, publications).
- **`web_answer`** asks the provider for a short answer with the sources it used. Exa and Tavily have
  it; SearXNG does not.
- **`web_fetch`** reads one page as text. Long pages come in parts, and the bot asks for the next part
  when it needs it. It works even with no provider set up.

The bot is told that a page's text is the page's, not yours, and never to follow instructions found in
it.

## Setting it up

**App Settings → Web search.** An admin picks the provider everyone's bots use:

| Provider | What it needs | What it adds |
|---|---|---|
| [Exa](https://exa.ai) | An API key | Categories (news, companies, people, publications), a deep mode, answers with sources |
| [Tavily](https://tavily.com) | An API key (`tvly-…`) | A news and a finance topic, a deeper mode, answers with sources |
| [SearXNG](https://docs.searxng.org) | Your server's address | Free and self-hosted; turn on the JSON format (`json` in `search.formats` in its `settings.yml`) |

**Test** runs a small search with what you typed, before you save it, and says what came back and what it
cost.

Under **Your own (optional)**, any account can set a provider and key of its own; its bots use that
instead of everyone's. A teammate using a bot you shared goes through yours.

Each bot's **Tools** page has a **Web search** group to switch off for a bot that should not use the web.

## Reading pages

With Exa or Tavily, `web_fetch` reads a page through the provider, which also handles pages built by
scripts and PDFs; if the provider cannot read it, Cubicle reads it directly. With SearXNG or no provider,
Cubicle reads the page itself.

Cubicle reads only public addresses itself: a name that leads to your own network (`192.168.x.x`,
`localhost`, other containers) is refused, because the Cubicle server can reach things a bot's computer
should not. For a page on your network, the bot uses its computer (`curl`, or the browser).

## What never leaves

A search, a question or an address that holds the value of one of the bot's
[secrets](secrets.md) is refused before anything is sent, so a bot cannot paste a token into a search
engine. What a bot searches for is otherwise sent to the provider you chose, under its terms.
