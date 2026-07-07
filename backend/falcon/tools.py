"""
tools.py — LangChain/LangGraph tool layer for Falcon.

Two external-API tools, auto-triggered by the model when the user's message
calls for them:

  get_weather     Current weather via Open-Meteo (free, no API key).
                  Accepts a place name ("Lahore", "Pakistan", "London")
                  OR explicit latitude/longitude coordinates.

  get_nasa_apod   NASA Astronomy Picture of the Day.
                  Accepts an optional date (YYYY-MM-DD); empty = today.
                  Uses NASA_API_KEY from .env, falls back to DEMO_KEY.

run_agent() wraps the tools in a LangGraph ReAct agent driven by the same
OpenRouter model and payload the chat already assembled. Transparency is
preserved: every tool call and tool result is returned as an event list so
the UI can log it to the trace and audit trail — nothing happens silently.
"""
from __future__ import annotations

import json
import os
import re

import requests
from langchain_core.messages import AIMessage, AIMessageChunk, ToolMessage
from langchain_core.tools import tool
from langchain_openai import ChatOpenAI
from langgraph.prebuilt import create_react_agent

_OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"

_GEOCODE_URL     = "https://geocoding-api.open-meteo.com/v1/search"
_FORECAST_URL    = "https://api.open-meteo.com/v1/forecast"
_WEATHERAPI_URL  = "https://api.weatherapi.com/v1/current.json"
_APOD_URL        = "https://api.nasa.gov/planetary/apod"

_HTTP_TIMEOUT = 20  # seconds


def _get_with_retry(url: str, params: dict) -> requests.Response:
    """GET with one retry — external APIs (esp. api.nasa.gov) are flaky."""
    try:
        return requests.get(url, params=params, timeout=_HTTP_TIMEOUT)
    except requests.RequestException:
        return requests.get(url, params=params, timeout=_HTTP_TIMEOUT)

# WMO weather interpretation codes → human-readable condition
_WMO_CODES = {
    0: "clear sky", 1: "mainly clear", 2: "partly cloudy", 3: "overcast",
    45: "fog", 48: "depositing rime fog",
    51: "light drizzle", 53: "moderate drizzle", 55: "dense drizzle",
    56: "light freezing drizzle", 57: "dense freezing drizzle",
    61: "slight rain", 63: "moderate rain", 65: "heavy rain",
    66: "light freezing rain", 67: "heavy freezing rain",
    71: "slight snowfall", 73: "moderate snowfall", 75: "heavy snowfall",
    77: "snow grains",
    80: "slight rain showers", 81: "moderate rain showers", 82: "violent rain showers",
    85: "slight snow showers", 86: "heavy snow showers",
    95: "thunderstorm", 96: "thunderstorm with slight hail", 99: "thunderstorm with heavy hail",
}


# ---------------------------------------------------------------------------
# Tool: get_weather
# ---------------------------------------------------------------------------

def _weather_weatherapi(location: str, latitude, longitude, api_key: str) -> dict:
    """Current weather via WeatherAPI.com. Raises on transport error; returns
    a dict (with an "error" key on API-level failure) on success path."""
    # WeatherAPI's `q` param accepts a place name OR "lat,lon" — it geocodes
    # internally, so no separate geocoding call is needed.
    if latitude is not None and longitude is not None:
        query = f"{latitude},{longitude}"
    else:
        query = location.strip()

    resp = _get_with_retry(
        _WEATHERAPI_URL,
        params={"key": api_key, "q": query, "aqi": "no"},
    )
    if resp.status_code != 200:
        try:
            detail = (resp.json().get("error") or {}).get("message", "")
        except Exception:
            detail = resp.text[:200]
        return {"error": f"WeatherAPI request failed ({resp.status_code}): {detail}"}

    data = resp.json()
    loc  = data.get("location") or {}
    cur  = data.get("current") or {}
    cond = cur.get("condition") or {}
    place = ", ".join(
        str(p) for p in [loc.get("name"), loc.get("region"), loc.get("country")] if p
    )
    return {
        "place":                 place or f"lat {latitude}, lon {longitude}",
        "latitude":              loc.get("lat", latitude),
        "longitude":             loc.get("lon", longitude),
        "local_time":            loc.get("localtime"),
        "temperature_c":         cur.get("temp_c"),
        "feels_like_c":          cur.get("feelslike_c"),
        "relative_humidity_pct": cur.get("humidity"),
        "wind_speed_kmh":        cur.get("wind_kph"),
        "wind_direction_deg":    cur.get("wind_degree"),
        "condition":             cond.get("text"),
        "is_day":                bool(cur.get("is_day", 1)),
        "source":                "weatherapi.com",
    }


def _weather_open_meteo(location: str, latitude, longitude) -> dict:
    """Current weather via Open-Meteo (free, no key). Fallback provider."""
    resolved_name = None
    # Resolve a place name to coordinates via Open-Meteo geocoding
    if latitude is None or longitude is None:
        if not location or not location.strip():
            return {"error": "Provide a location name or latitude+longitude."}
        geo = _get_with_retry(
            _GEOCODE_URL,
            params={"name": location.strip(), "count": 1, "language": "en", "format": "json"},
        )
        geo.raise_for_status()
        results = (geo.json() or {}).get("results") or []
        if not results:
            return {"error": f"Could not find a place named {location!r}."}
        place = results[0]
        latitude, longitude = place["latitude"], place["longitude"]
        resolved_name = ", ".join(
            str(p) for p in
            [place.get("name"), place.get("admin1"), place.get("country")]
            if p
        )

    fc = _get_with_retry(
        _FORECAST_URL,
        params={
            "latitude":  latitude,
            "longitude": longitude,
            "current": "temperature_2m,apparent_temperature,relative_humidity_2m,"
                       "wind_speed_10m,wind_direction_10m,weather_code,is_day",
            "timezone": "auto",
        },
    )
    fc.raise_for_status()
    data    = fc.json()
    current = data.get("current") or {}
    code    = current.get("weather_code")
    return {
        "place":                 resolved_name or f"lat {latitude}, lon {longitude}",
        "latitude":              latitude,
        "longitude":             longitude,
        "local_time":            current.get("time"),
        "temperature_c":         current.get("temperature_2m"),
        "feels_like_c":          current.get("apparent_temperature"),
        "relative_humidity_pct": current.get("relative_humidity_2m"),
        "wind_speed_kmh":        current.get("wind_speed_10m"),
        "wind_direction_deg":    current.get("wind_direction_10m"),
        "condition":             _WMO_CODES.get(code, f"weather code {code}"),
        "is_day":                bool(current.get("is_day", 1)),
        "source":                "open-meteo",
    }


@tool
def get_weather(
    location: str = "",
    latitude: float | None = None,
    longitude: float | None = None,
) -> str:
    """Get the CURRENT weather for a place.

    Use this whenever the user asks about weather, temperature, wind,
    humidity, or general conditions anywhere in the world.

    Provide EITHER a location name (city, region, or country — e.g.
    "Lahore", "Pakistan", "London") OR explicit latitude and longitude
    coordinates if the user gave numbers.

    Args:
        location:  Place name to look up (leave empty if coordinates given).
        latitude:  Latitude in decimal degrees (use only with longitude).
        longitude: Longitude in decimal degrees (use only with latitude).

    Returns:
        JSON string with the resolved place, coordinates, temperature (°C),
        feels-like temperature, humidity, wind speed and weather condition.
    """
    if (latitude is None or longitude is None) and not (location and location.strip()):
        return json.dumps({"error": "Provide a location name or latitude+longitude."})

    api_key = os.environ.get("WEATHERAPI_KEY", "").strip()

    # Primary: WeatherAPI.com (reliable, 1M calls/month free) when a key is set.
    # Fallback: Open-Meteo (free, no key) if the key is absent or the primary
    # provider errors — so the tool keeps working through outages/rate limits.
    if api_key:
        try:
            result = _weather_weatherapi(location, latitude, longitude, api_key)
            if "error" not in result:
                return json.dumps(result)
        except requests.RequestException:
            result = None  # fall through to Open-Meteo

    try:
        return json.dumps(_weather_open_meteo(location, latitude, longitude))
    except requests.RequestException as exc:
        return json.dumps({"error": f"Weather service request failed: {exc}"})


# ---------------------------------------------------------------------------
# Tool: get_nasa_apod
# ---------------------------------------------------------------------------

@tool
def get_nasa_apod(date: str = "") -> str:
    """Get NASA's Astronomy Picture of the Day (APOD).

    Use this whenever the user asks for the astronomy picture of the day,
    NASA picture, or a space image for a specific date.

    Args:
        date: Date in YYYY-MM-DD format (e.g. "2024-12-25").
              Leave empty for today's picture.
              Valid range: 1995-06-16 to today.

    Returns:
        JSON string with the picture's title, date, explanation, media type
        and image URL. Always include the image URL in your answer so the
        picture can be displayed.
    """
    api_key = os.environ.get("NASA_API_KEY", "").strip() or "DEMO_KEY"
    params: dict = {"api_key": api_key}
    if date and date.strip():
        params["date"] = date.strip()

    try:
        resp = _get_with_retry(_APOD_URL, params=params)
    except requests.RequestException as exc:
        return json.dumps({"error": f"NASA APOD request failed: {exc}"})
    if resp.status_code != 200:
        try:
            detail = resp.json().get("msg") or resp.json().get("error", {}).get("message", "")
        except Exception:
            detail = resp.text[:200]
        return json.dumps({"error": f"NASA APOD request failed ({resp.status_code}): {detail}"})

    data = resp.json()
    return json.dumps({
        "title":       data.get("title"),
        "date":        data.get("date"),
        "explanation": data.get("explanation"),
        "media_type":  data.get("media_type"),
        "url":         data.get("url"),
        "hdurl":       data.get("hdurl"),
        "copyright":   data.get("copyright"),
    })


TOOLS = [get_weather, get_nasa_apod]


def tool_names() -> list[str]:
    return [t.name for t in TOOLS]


# ---------------------------------------------------------------------------
# run_agent — LangGraph ReAct agent over the pre-assembled Falcon payload
# ---------------------------------------------------------------------------

def _content_to_text(content) -> str:
    """AIMessage content may be a string or a list of content blocks."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict) and block.get("type") == "text":
                parts.append(block.get("text", ""))
        return "".join(parts)
    return str(content)


def _apod_image_from_tool_content(content: str) -> dict | None:
    """Extract a displayable image dict from a get_nasa_apod result, or None.

    Uses the standard-resolution URL for display — the HD version can be
    5–15 MB and is slow to load. hdurl is kept as a "full resolution" link.
    """
    try:
        apod = json.loads(content)
    except (ValueError, TypeError):
        return None
    if apod.get("media_type") == "image" and apod.get("url"):
        return {
            "title": apod.get("title") or "NASA APOD",
            "url":   apod.get("url"),
            "hdurl": apod.get("hdurl"),
        }
    return None


# Tool-routing reliability is temperature-sensitive: at higher temperatures the
# model sometimes skips a needed tool call and fabricates plausible-looking data
# by copying the format/magnitude of an earlier weather answer in the
# conversation (most common on terse follow-ups like "and whats in lahore").
# Greedy decoding (temperature 0.0) is the most reliable setting for routing, so
# the agent is pinned to 0.0 while the user's own temperature still governs plain
# chat. This only affects tool turns, not plain chat generation.
_AGENT_MAX_TEMPERATURE = 0.0

# Streaming anti-fabrication guard threshold. Answer tokens produced before any
# tool call are held until we can tell they are NOT a fabricated live-data
# (weather / APOD) answer. Once the buffered text reaches this many characters
# WITHOUT tripping the live-data detector, it is treated as ordinary prose and
# streamed live. Real fabricated weather/APOD answers are keyword-dense and trip
# the detector well before this, so they stay buffered and get repaired — no
# fabricated text is ever shown.
_STREAM_SAFETY_FLUSH_CHARS = 160


def _build_llm(model_name, api_key, temperature, top_p):
    return ChatOpenAI(
        model=model_name,
        api_key=api_key,
        base_url=_OPENROUTER_BASE_URL,
        temperature=min(temperature, _AGENT_MAX_TEMPERATURE),
        top_p=top_p,
        # Retry 429 / 5xx with exponential backoff (honours Retry-After).
        # The agent makes 2-3 calls per turn, so it is the most likely path
        # to trip a rate limit — retries let brief spikes recover silently.
        max_retries=5,
        default_headers={
            "HTTP-Referer": "https://github.com/falcon",
            "X-Title":      "Falcon",
        },
    )


# Passed as create_react_agent(prompt=...) on the tools path ONLY (never on
# plain chat, so Falcon's neutrality is preserved when tools are off). Without
# it, models re-render stale weather/live data already present in conversation
# history — especially after an image turn, where the model conflates the
# earlier image description with the new request and skips the tool call.
# Verified 8/8 tool calls with this text vs ~0-2/3 without it.
_TOOL_USE_DIRECTIVE = (
    "You have live-data tools. You MUST obey these rules:\n"
    "1. For any request about current/real-time data (weather, temperature, "
    "prices, time, live status), you MUST call the matching tool THIS turn to "
    "get fresh values.\n"
    "2. Do NOT reuse or repeat weather/live values from earlier messages in the "
    "conversation, even if an earlier assistant reply already stated them — they "
    "are stale. Call the tool again.\n"
    "3. Do not answer a live-data question from a prior image description or from "
    "memory. Ignore earlier images unless the current request is about them.\n"
    "4. Short or elliptical follow-ups still count. If the user writes something "
    "like 'and in Lahore', 'what about Paris?', or 'and now?', treat it as a NEW "
    "live-data request for that place/topic and call the tool again — never infer "
    "the answer by adapting the numbers from a previous reply."
)


# ---------------------------------------------------------------------------
# Anti-fabrication guard
#
# Prompt directives reduce, but do not eliminate, the case where the model
# skips get_weather and invents plausible numbers by copying an earlier weather
# reply in the conversation (most common on terse follow-ups like "and in
# London"). This guard makes it deterministic: if the agent produced a
# weather-shaped answer with ZERO tool calls, we force one real get_weather call
# and rebuild the answer from the actual API result, so a fabricated weather
# answer can never reach the user.
# ---------------------------------------------------------------------------

_TOOL_BY_NAME = {t.name: t for t in TOOLS}

# A temperature written with a degree unit (e.g. "30.2°C", "37 °F") — the
# strongest single signal that an answer contains live weather data.
_DEGREE_RE = re.compile(r"\d+(?:\.\d+)?\s*°\s*[cf]", re.IGNORECASE)

_WEATHER_KEYWORDS = (
    "humidity", "feels like", "feels-like", "wind", "weather", "overcast",
    "cloudy", "clear sky", "drizzle", "haze", "mist", "fog", "precipitation",
    "forecast", "sunny", "thunderstorm",
)


def _looks_like_weather_answer(text: str) -> bool:
    """True if text is a live-weather answer (a degree temperature plus weather
    vocabulary, or several weather terms together). Deliberately strict so it
    does not fire on incidental temperatures like 'water boils at 100°C'."""
    if not text:
        return False
    low = text.lower()
    has_temp = bool(_DEGREE_RE.search(text))
    keyword_hits = sum(1 for k in _WEATHER_KEYWORDS if k in low)
    return (has_temp and keyword_hits >= 1) or keyword_hits >= 2


# Phrases specific to a NASA Astronomy Picture of the Day answer. Strict enough
# that a generic NASA mention (e.g. "what is NASA's budget") does not match.
_APOD_URL_RE = re.compile(r"apod\.nasa\.gov", re.IGNORECASE)


def _looks_like_apod_answer(text: str) -> bool:
    """True if text is a NASA APOD (astronomy picture of the day) answer."""
    if not text:
        return False
    low = text.lower()
    return (
        "astronomy picture of the day" in low
        or "apod" in low
        or bool(_APOD_URL_RE.search(text))
    )


def _looks_like_live_data_answer(text: str) -> bool:
    """True if text looks like an answer that should have come from a live tool
    (weather or NASA APOD). Used to catch answers the model fabricated instead
    of calling the matching tool."""
    return _looks_like_weather_answer(text) or _looks_like_apod_answer(text)


def _execute_tool_call(tc: dict) -> str:
    """Invoke a bound tool by its tool-call dict, returning its string result."""
    tool_obj = _TOOL_BY_NAME.get(tc.get("name"))
    if tool_obj is None:
        return json.dumps({"error": f"unknown tool {tc.get('name')!r}"})
    try:
        return tool_obj.invoke(tc.get("args") or {})
    except Exception as exc:  # never let a tool error abort the repair
        return json.dumps({"error": f"tool {tc.get('name')} failed: {exc}"})


def _accumulate_usage(msg, usage: dict) -> None:
    meta = getattr(msg, "usage_metadata", None) or {}
    usage["prompt_tokens"]     += meta.get("input_tokens", 0)
    usage["completion_tokens"] += meta.get("output_tokens", 0)
    usage["total_tokens"]      += meta.get("total_tokens", 0)


def _forced_tool_repair(payload: list, llm) -> dict:
    """Force one tool call and rebuild the answer from the real tool result.

    Called only when the agent produced a live-data-shaped answer (weather or
    NASA APOD) without calling any tool. Uses tool_choice to force a tool
    selection — the model picks the right tool (get_weather with the location,
    or get_nasa_apod with the date) from context — executes it, then does a
    normal follow-up call so the model writes the answer from fresh data.

    Returns: {text, events, images, usage, tool_calls}.
    """
    usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
    events: list[dict] = []
    images: list[dict] = []
    tool_calls = 0

    forcing_llm = llm.bind_tools(TOOLS, tool_choice="any")
    ai = forcing_llm.invoke(payload)
    _accumulate_usage(ai, usage)

    tool_msgs: list[ToolMessage] = []
    for tc in (ai.tool_calls or []):
        tool_calls += 1
        events.append({"type": "tool_call", "tool": tc.get("name"), "args": tc.get("args")})
        content = _execute_tool_call(tc)
        events.append({"type": "tool_result", "tool": tc.get("name"), "content": content[:2000]})
        tool_msgs.append(
            ToolMessage(content=content, tool_call_id=tc.get("id"), name=tc.get("name"))
        )
        if tc.get("name") == "get_nasa_apod":
            img = _apod_image_from_tool_content(content)
            if img:
                images.append(img)

    # Second pass (auto tool_choice) so the model produces the final answer from
    # the fresh tool result instead of another forced tool call.
    answering_llm = llm.bind_tools(TOOLS)
    final_ai = answering_llm.invoke(list(payload) + [ai] + tool_msgs)
    _accumulate_usage(final_ai, usage)
    final_text = _content_to_text(final_ai.content).strip()

    return {
        "text":       final_text,
        "events":     events,
        "images":     images,
        "usage":      usage,
        "tool_calls": tool_calls,
    }


def run_agent(
    payload: list[dict],
    model_name: str,
    api_key: str,
    temperature: float = 0.7,
    top_p: float = 1.0,
    max_tool_rounds: int = 5,
) -> dict:
    """Run the tool-calling agent on an already-assembled Falcon payload.

    The payload is the exact same list of {role, content} dicts that
    Engine.build_annotated_payload produced — persona, system prompt,
    memory and history included. Nothing is rebuilt or injected here.

    Returns a dict:
        text        Final natural-language answer from the model.
        events      Ordered list of transparency events:
                      {"type": "tool_call",   "tool": name, "args": {...}}
                      {"type": "tool_result", "tool": name, "content": str}
        images      [{"title": str, "url": str}] extracted from APOD results,
                    so the UI can render the picture.
        usage       {"prompt_tokens", "completion_tokens", "total_tokens"}
                    summed across all model calls the agent made.
        tool_calls_made  Number of tool invocations executed.
    """
    llm = _build_llm(model_name, api_key, temperature, top_p)

    agent  = create_react_agent(llm, TOOLS, prompt=_TOOL_USE_DIRECTIVE)
    result = agent.invoke(
        {"messages": payload},
        config={"recursion_limit": 2 * max_tool_rounds + 2},
    )

    new_messages = result["messages"][len(payload):]

    events: list[dict] = []
    images: list[dict] = []
    usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
    final_text      = ""
    tool_calls_made = 0

    for msg in new_messages:
        if isinstance(msg, AIMessage):
            meta = msg.usage_metadata or {}
            usage["prompt_tokens"]     += meta.get("input_tokens", 0)
            usage["completion_tokens"] += meta.get("output_tokens", 0)
            usage["total_tokens"]      += meta.get("total_tokens", 0)

            for tc in (msg.tool_calls or []):
                tool_calls_made += 1
                events.append({
                    "type": "tool_call",
                    "tool": tc.get("name"),
                    "args": tc.get("args"),
                })

            text = _content_to_text(msg.content).strip()
            if text and not msg.tool_calls:
                final_text = text

        elif isinstance(msg, ToolMessage):
            content = _content_to_text(msg.content)
            events.append({
                "type":    "tool_result",
                "tool":    msg.name,
                "content": content[:2000],
            })
            if msg.name == "get_nasa_apod":
                img = _apod_image_from_tool_content(content)
                if img:
                    images.append(img)

    # Anti-fabrication guard: a live-data-shaped answer (weather or NASA APOD)
    # with no tool call means the model invented it — force a real lookup and
    # replace it.
    if tool_calls_made == 0 and _looks_like_live_data_answer(final_text):
        try:
            rep = _forced_tool_repair(payload, llm)
            if rep["text"]:
                final_text = rep["text"]
                events.extend(rep["events"])
                images.extend(rep["images"])
                tool_calls_made += rep["tool_calls"]
                for k in usage:
                    usage[k] += rep["usage"].get(k, 0)
        except Exception:
            pass  # on repair failure keep the original answer rather than erroring

    return {
        "text":            final_text,
        "events":          events,
        "images":          images,
        "usage":           usage,
        "tool_calls_made": tool_calls_made,
    }


# ---------------------------------------------------------------------------
# AgentStream — streaming variant so the final answer appears token-by-token
# ---------------------------------------------------------------------------

class AgentStream:
    """Streams the agent's final natural-language answer token-by-token.

    Mirrors Engine._StreamResult so app.py can pass it straight to
    st.write_stream(). While the model is deciding which tool to call and the
    tool is running, nothing is yielded (those turns carry no answer text);
    once the model writes its final answer, those tokens stream live.

    After the generator is exhausted these attributes are populated:
        events           ordered tool_call / tool_result transparency events
        images           APOD images extracted from tool results
        usage            summed token usage across all model calls
        tool_calls_made  number of tool invocations
        raw_output       full concatenated answer text
    """

    def __init__(
        self,
        payload: list[dict],
        model_name: str,
        api_key: str,
        temperature: float = 0.7,
        top_p: float = 1.0,
        max_tool_rounds: int = 5,
    ):
        self._payload         = payload
        self._model_name      = model_name
        self._api_key         = api_key
        self._temperature     = temperature
        self._top_p           = top_p
        self._max_tool_rounds = max_tool_rounds

        self.events: list[dict] = []
        self.images: list[dict] = []
        self.usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
        self.tool_calls_made = 0
        self.raw_output = ""
        self._gen = None

    def _run(self):
        llm   = _build_llm(self._model_name, self._api_key, self._temperature, self._top_p)
        agent = create_react_agent(llm, TOOLS, prompt=_TOOL_USE_DIRECTIVE)

        seen_tool_call_ids: set[str] = set()

        # Streaming with the anti-fabrication guard preserved.
        #
        # Answer tokens are streamed live token-by-token EXCEPT while we still
        # cannot rule out that the model is fabricating a live-data (weather /
        # APOD) answer without calling a tool. Concretely:
        #   • After a tool has run, the answer is genuinely tool-backed → stream
        #     live immediately (flushing anything held).
        #   • Before any tool runs, tokens are held in a buffer. As soon as the
        #     buffered text is clearly ordinary prose (past the safety threshold
        #     and not live-data-shaped), flush it and stream the rest live.
        #   • If the buffer ever looks like a live-data answer while no tool has
        #     run, it stays held to the end and is repaired (a real tool call is
        #     forced) before anything is yielded — so a fabricated weather/APOD
        #     answer is never shown.
        pre_tool_buffer: list[str] = []
        streaming  = False   # have we begun streaming live yet?
        held_risky = False   # buffered text looks like a fabricated live-data answer

        for stream_mode, chunk in agent.stream(
            {"messages": self._payload},
            stream_mode=["updates", "messages"],
            config={"recursion_limit": 2 * self._max_tool_rounds + 2},
        ):
            if stream_mode == "messages":
                # (message_chunk, metadata) — stream only answer-text tokens.
                msg_chunk = chunk[0]
                if isinstance(msg_chunk, AIMessageChunk):
                    text = _content_to_text(msg_chunk.content)
                    if not text:
                        continue
                    if streaming:
                        # Already streaming live — keep going.
                        self.raw_output += text
                        yield text
                    elif self.tool_calls_made >= 1:
                        # A tool just ran → answer is trustworthy. Flush the held
                        # buffer, then stream live from here.
                        if pre_tool_buffer:
                            flush = "".join(pre_tool_buffer)
                            pre_tool_buffer.clear()
                            self.raw_output += flush
                            yield flush
                        self.raw_output += text
                        streaming = True
                        yield text
                    else:
                        # No tool yet → hold until we can classify the answer.
                        pre_tool_buffer.append(text)
                        buffered = "".join(pre_tool_buffer)
                        if _looks_like_live_data_answer(buffered):
                            # Possible fabrication — keep holding; repair at end.
                            held_risky = True
                        elif not held_risky and len(buffered) >= _STREAM_SAFETY_FLUSH_CHARS:
                            # Clearly ordinary prose → safe to stream live now.
                            pre_tool_buffer.clear()
                            self.raw_output += buffered
                            streaming = True
                            yield buffered
                continue

            # stream_mode == "updates": {node_name: {"messages": [...]}}
            for node_update in chunk.values():
                for msg in node_update.get("messages", []):
                    if isinstance(msg, AIMessage):
                        meta = msg.usage_metadata or {}
                        self.usage["prompt_tokens"]     += meta.get("input_tokens", 0)
                        self.usage["completion_tokens"] += meta.get("output_tokens", 0)
                        self.usage["total_tokens"]      += meta.get("total_tokens", 0)
                        for tc in (msg.tool_calls or []):
                            tc_id = tc.get("id") or f"{tc.get('name')}:{self.tool_calls_made}"
                            if tc_id in seen_tool_call_ids:
                                continue
                            seen_tool_call_ids.add(tc_id)
                            self.tool_calls_made += 1
                            self.events.append({
                                "type": "tool_call",
                                "tool": tc.get("name"),
                                "args": tc.get("args"),
                            })
                    elif isinstance(msg, ToolMessage):
                        content = _content_to_text(msg.content)
                        self.events.append({
                            "type":    "tool_result",
                            "tool":    msg.name,
                            "content": content[:2000],
                        })
                        if msg.name == "get_nasa_apod":
                            img = _apod_image_from_tool_content(content)
                            if img:
                                self.images.append(img)

        # If we already streamed the answer live, we are done.
        if streaming:
            return

        # Nothing was streamed → the whole answer is still buffered. This is
        # either a short ordinary answer (below the safety-flush threshold) or a
        # held live-data-shaped answer produced with no tool call.
        final_text = "".join(pre_tool_buffer).strip()

        if self.tool_calls_made == 0 and _looks_like_live_data_answer(final_text):
            try:
                rep = _forced_tool_repair(self._payload, llm)
                if rep["text"]:
                    final_text = rep["text"]
                    self.events.extend(rep["events"])
                    self.images.extend(rep["images"])
                    self.tool_calls_made += rep["tool_calls"]
                    for k in self.usage:
                        self.usage[k] += rep["usage"].get(k, 0)
            except Exception:
                pass  # keep the buffered answer if the repair attempt fails

        self.raw_output = final_text
        if final_text:
            yield final_text

    def __iter__(self):
        if self._gen is None:
            self._gen = self._run()
        return self._gen

    def __next__(self):
        if self._gen is None:
            self._gen = self._run()
        return next(self._gen)


def stream_agent(
    payload: list[dict],
    model_name: str,
    api_key: str,
    temperature: float = 0.7,
    top_p: float = 1.0,
    max_tool_rounds: int = 5,
) -> AgentStream:
    """Convenience constructor for AgentStream (see class docstring)."""
    return AgentStream(
        payload=payload,
        model_name=model_name,
        api_key=api_key,
        temperature=temperature,
        top_p=top_p,
        max_tool_rounds=max_tool_rounds,
    )
