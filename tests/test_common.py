from work_history.collectors.common import adf_to_text, html_to_text


def test_content_conversion() -> None:
    assert (
        adf_to_text(
            {
                "type": "doc",
                "content": [
                    {
                        "type": "paragraph",
                        "content": [{"type": "text", "text": "Hello"}],
                    }
                ],
            }
        ).strip()
        == "Hello"
    )
    assert html_to_text("<p>Hello <strong>world</strong></p>") == "Hello\nworld"
