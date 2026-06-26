from app.scrapers.base import Scraper
from app.scrapers.kijiji import KijijiScraper
from app.scrapers.rentals_ca import RentalsCaScraper
from app.scrapers.rentfaster import RentFasterScraper
from app.scrapers.zumper import ZumperScraper

# Registered scrapers, added as they're implemented.
SCRAPERS: list[Scraper] = [
    RentFasterScraper(),
    RentalsCaScraper(),
    ZumperScraper(),
    KijijiScraper(),
]
