"""
Web Request Tool - Fetch content from a web URL.
"""

from typing import Dict, Optional, Union
from declarative_agent_sdk.agent_logging import get_logger

logger = get_logger(__name__)


def web_request(
    url: str,
    method: str = "GET",
    headers: Optional[Dict[str, str]] = None,
    body: Optional[str] = None,
    timeout: int = 30,
    follow_redirects: bool = True,
    extract_text: bool = True,
) -> Dict[str, Union[str, int, bool, None]]:
    """
    Fetch content from a web URL.

    Args:
        url: The URL to request
        method: HTTP method (GET, POST, PUT, DELETE, HEAD). Defaults to GET
        headers: Optional dictionary of HTTP headers to include
        body: Optional request body string (for POST/PUT)
        timeout: Request timeout in seconds (default: 30)
        follow_redirects: Follow HTTP redirects (default: True)
        extract_text: When True and response is HTML, strip tags and return plain
                      text instead of raw HTML (default: True)

    Returns:
        Dictionary containing:
            - success: bool - Whether request completed with a 2xx status code
            - status_code: int - HTTP response status code
            - content: str - Response body (plain text when extract_text=True, raw otherwise)
            - content_type: str - Value of the Content-Type response header
            - url: str - Final URL after any redirects
            - error: str or None - Error message if request failed
    """
    result: Dict[str, Union[str, int, bool, None]] = {
        "success": False,
        "status_code": None,
        "content": "",
        "content_type": "",
        "url": url,
        "error": None,
    }

    try:
        import httpx
    except ImportError:
        result["error"] = "httpx is not installed. Run: pip install httpx"
        logger.error(result["error"])
        return result

    try:
        logger.debug(f"web_request {method} {url} (timeout={timeout}s)")

        with httpx.Client(follow_redirects=follow_redirects, timeout=timeout) as client:
            response = client.request(
                method=method.upper(),
                url=url,
                headers=headers or {},
                content=body.encode() if body else None,
            )

        result["status_code"] = response.status_code
        result["url"] = str(response.url)
        content_type = response.headers.get("content-type", "")
        result["content_type"] = content_type
        result["success"] = response.is_success

        raw_text = response.text

        if extract_text and "html" in content_type.lower():
            result["content"] = _strip_html(raw_text)
        else:
            result["content"] = raw_text

        if not result["success"]:
            logger.warning(f"web_request returned HTTP {response.status_code} for {url}")
        else:
            logger.info(f"web_request succeeded: {method} {url} -> {response.status_code}")

    except httpx.TimeoutException:
        result["error"] = f"Request timed out after {timeout} seconds"
        logger.error(f"web_request timed out: {url}")

    except httpx.InvalidURL as e:
        result["error"] = f"Invalid URL: {e}"
        logger.error(f"web_request invalid URL '{url}': {e}")

    except httpx.RequestError as e:
        result["error"] = f"Request error: {e}"
        logger.error(f"web_request error for '{url}': {e}", exc_info=True)

    except Exception as e:
        result["error"] = f"Unexpected error: {e}"
        logger.error(f"web_request unexpected error for '{url}': {e}", exc_info=True)

    return result


def _strip_html(html: str) -> str:
    """Remove HTML tags and collapse whitespace."""
    import re

    # Remove <script> and <style> blocks entirely
    html = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", html, flags=re.DOTALL | re.IGNORECASE)
    # Remove remaining tags
    html = re.sub(r"<[^>]+>", " ", html)
    # Decode common HTML entities
    html = (
        html.replace("&amp;", "&")
        .replace("&lt;", "<")
        .replace("&gt;", ">")
        .replace("&quot;", '"')
        .replace("&#39;", "'")
        .replace("&nbsp;", " ")
    )
    # Collapse whitespace
    html = re.sub(r"\s+", " ", html).strip()
    return html
