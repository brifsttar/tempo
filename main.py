import argparse
from datetime import datetime as dt
from random import randrange
import logging as log
import time
import re

from playwright.sync_api import sync_playwright
import keyring
import discord_webhook

def get_badgeage_times(in_badgeage_times, random_offset_range):
    now = dt.now()
    for h in in_badgeage_times:
        # Offset in minutes
        offset = randrange(random_offset_range) - (random_offset_range / 2.)
        # To hour
        h += offset / 60
        yield now.replace(hour=int(h), minute=int(60 * (h % 1)), second=0)

def main():
    log.basicConfig(
        format='%(asctime)-15s [%(levelname)-8s]: %(message)s',
        level=log.INFO,
        handlers=[
            log.FileHandler("tempo.log"),
            log.StreamHandler(),
        ]
    )
    parser = argparse.ArgumentParser(formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    parser.add_argument(
        'username',
        type=str,
        help='Username for tempo.univ-eiffel.fr, as stored on the local keyring'
    )
    parser.add_argument(
        '--badgeage-times', '-b',
        nargs=4,
        type=float,
        metavar=("Entrée-matin", "Sortie-midi", "Entrée-après-midi", "Sortie-soir"),
        default=[8.75, 12, 12.75, 18],
        help='Times around which to badge'
    )
    parser.add_argument(
        '--random-offset-range', '-r',
        type=int,
        default=30,
        help='Range in minutes for random offset around badgeage times'
    )
    parser.add_argument(
        "--discord-webhook-url", '-d',
        type=str,
        default=None,
        help="Discord webhook URL for notifications"
    )
    args = parser.parse_args()
    badgeage_times = list(get_badgeage_times(args.badgeage_times, args.random_offset_range))
    now = dt.now()
    clock_out = badgeage_times[-1]
    is_errored = False
    if now > clock_out:
        log.info(f"{now} is after end of day")
        return
    if now.weekday() > 4:
        log.info(f"{now} is not a working day")
        return
    while True:
        last_error = None
        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=True)
                ctx = browser.new_context()
                page = ctx.new_page()

                # 1. Login on CAS
                page.goto("http://tempo.univ-eiffel.fr/")
                page.fill("#username", args.username)
                page.fill("#password", keyring.get_password("tempo", args.username))
                page.click('button[type="submit"]')
                page.wait_for_url("**/webquartz/ux/home")

                # 2. Check if the day has a legitimate "absence"
                page.get_by_role("button", name="Menu").click()
                page.get_by_role("menuitem").click()
                page.locator("#menu-item-3").click()

                # 2.1 Select the current week
                period = page.locator("[class*='period-planning-picker']")
                period.locator("[class*='variant-action']").click()
                calendar = page.locator("[class*='period-planning-action-buttons']")
                calendar.locator("[class*='variant-action']").click()

                # 2.2 Find current day index
                header = page.locator("[class*='planning-header']")
                current_day = header.locator("[class*='current'][class*='selectable']")
                parent =  current_day.locator("..").element_handle()
                current_handle = current_day.element_handle()
                today = parent.evaluate("(parent, el) => Array.from(parent.children).indexOf(el)", current_handle)

                # 2.3 Check for absences on current day
                planning = page.locator("[class*='planning-individual']")
                absences_line = planning.locator('[data-dragcontext="absence"]')
                absences = absences_line.locator("[class*='planning-item__abs']")
                for i in range(absences.count()):
                    element = absences.nth(i)
                    style = element.get_attribute("style")
                    match = re.search(r"grid-column:\s*(\d+)\s*/\s*(\d+)", style)
                    if match:
                        #TODO Handle half-day absences properly
                        absence_day_start = (int(match.group(1)) - 1) // 2
                        absence_day_end = (int(match.group(2)) - 1) // 2
                        if absence_day_start <= today <= absence_day_end:
                            log.info("Today has a legitimate absence, exiting")
                            browser.close()
                            return

                # 3. Navigate to get current badgeage status
                page.goto("http://tempo.univ-eiffel.fr/")
                page.get_by_role("button", name="Menu").click()
                page.get_by_role("menuitem").click()
                page.locator("#menu-item-6").click()
                page.locator(".clock-correction").wait_for(state="visible")
                grid = page.get_by_role("grid")
                rows = grid.get_by_role("row").count()

                # 4. Determine next badgeage time
                badgeages_count = rows - 1 if rows > 1 else 0
                if badgeages_count >= len(badgeage_times):
                    log.info("Done for the day")
                    browser.close()
                    return
                next_badgeage = badgeage_times[badgeages_count]
                now = dt.now()
                time_to = (next_badgeage - now).total_seconds()

                # 5. Badge or sleep
                if time_to < 0:
                    # Badgeage time!
                    page.goto("http://tempo.univ-eiffel.fr/")
                    page.get_by_role("button", name="Menu").click()
                    page.get_by_role("menuitem").click()
                    page.locator("#menu-item-5").click()
                    primary = page.locator("button[class*='button'][class*='primary']")
                    log.info("Badging")
                    primary.click()
                    page.wait_for_timeout(1000)
                    browser.close()
                else:
                    # Sleep until next badgeage
                    browser.close()
                    log.info(f"Sleeping {time_to}s until next badgeage at {next_badgeage}")
                    time.sleep(time_to)
        except (Exception,) as e:
            log.exception("An error occurred, retrying...")
            last_error = e
        else:
            last_error = None
        if last_error is not None and not is_errored:
            is_errored = True
            if args.discord_webhook_url:
                webhook = discord_webhook.DiscordWebhook(url=args.discord_webhook_url)
                embed = discord_webhook.DiscordEmbed(title="Tempo", color="ff0000")
                embed.add_embed_field(
                    name="Status",
                    value=f"An error occurred in tempo badgeage script for {args.username}, check logs for details.",
                    inline=False,
                )
                embed.add_embed_field(
                    name="Exception",
                    value=str(last_error),
                    inline=False,
                )
                webhook.add_embed(embed)
                webhook.execute()
        elif last_error is None and is_errored:
            is_errored = False
            if args.discord_webhook_url:
                webhook = discord_webhook.DiscordWebhook(url=args.discord_webhook_url)
                embed = discord_webhook.DiscordEmbed(
                    title="Tempo",
                    description=f"Error resolved in tempo badgeage script for {args.username}, back to normal operation.",
                    color="00ff00"
                )
                webhook.add_embed(embed)
                webhook.execute()


if __name__ == '__main__':
    main()
