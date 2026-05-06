from pathlib import Path

from playwright.sync_api import sync_playwright


FACEBOOK_LOGIN_URL = "https://www.facebook.com/login"
STATE_PATH = Path(__file__).resolve().parent / "facebook_state.json"


def main() -> None:
    print("Opening Chromium for manual Facebook login...")
    print(f"Storage state will be saved to: {STATE_PATH}")

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=False)
        context = browser.new_context()
        page = context.new_page()

        print(f"Opening login page: {FACEBOOK_LOGIN_URL}")
        page.goto(FACEBOOK_LOGIN_URL, wait_until="domcontentloaded", timeout=90000)

        input(
            "\nLog into Facebook in the opened browser window.\n"
            "When you are fully logged in, return here and press ENTER to save the session state..."
        )

        context.storage_state(path="facebook_state.json")
        print("\nSuccess: Facebook session state saved to facebook_state.json")

        context.close()
        browser.close()
        print("Browser closed cleanly.")


if __name__ == "__main__":
    main()
