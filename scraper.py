import requests
import os
from threading import Lock
import re
from bs4 import BeautifulSoup
from urllib.parse import urljoin, urlparse
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
import html2text
import logging 
import dotenv

# --- Configuration ---
BASE_URL = "https://pocketbase.io"
DOCS_ENTRY_URL = "https://pocketbase.io/docs/"
OUTPUT_DIR = "pocketbase_docs_llm"
INDEX_FILENAME = "llms.txt" # <--- Name for the index file
MAX_WORKERS = 10
REQUEST_DELAY = 0.1
SESSION = requests.Session()
FAILED_URLS_LOCK = Lock()
HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'
}

# --- Logging Setup ---
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
# Suppress noisy logs from libraries if needed
# logging.getLogger("requests").setLevel(logging.WARNING)
# logging.getLogger("urllib3").setLevel(logging.WARNING)


def sanitize_filename(name):
    """Removes invalid characters for filenames and replaces spaces/slashes."""
    name = name.strip('/ ')
    # Use pre-compiled patterns if available
    pattern1 = getattr(sanitize_filename, "pattern1", re.compile(r'[\\/]+'))
    pattern2 = getattr(sanitize_filename, "pattern2", re.compile(r'[^a-zA-Z0-9_\-]'))
    name = pattern1.sub('_', name)
    name = pattern2.sub('', name)
    if not name:
        name = "index"
    return name

def fetch_html(url):
    """Fetches HTML content for a given URL with error handling and session pooling."""
    try:
        response = SESSION.get(url, headers=HEADERS, timeout=20)
        response.raise_for_status()
        response.encoding = response.apparent_encoding or 'utf-8'
        # Only log at debug level to reduce I/O in tight loops
        logging.debug(f"Successfully fetched {url}")
        return response.text
    except requests.exceptions.Timeout:
        logging.error(f"Timeout fetching {url}")
        return None
    except requests.exceptions.HTTPError as e:
        logging.error(f"HTTP Error {e.response.status_code} fetching {url}: {e}")
        return None
    except requests.exceptions.RequestException as e:
        logging.error(f"Error fetching {url}: {e}")
        return None

# --- get_doc_links function remains the same ---
def get_doc_links(html_content, base_url):
    """Extracts all valid documentation links from the sidebar navigation."""
    soup = BeautifulSoup(html_content, 'html.parser')
    links = set()
    nav_links = []

    # Try <aside> first
    sidebar = soup.find('aside')
    if sidebar and (sidebar.find('nav') or sidebar.find('ul')):
        logging.info("Found potential sidebar <aside> element.")
        nav_links = sidebar.find_all('a', href=True)
    else:
        # Try <nav> elements
        logging.warning("Could not find a distinct <aside> sidebar. Searching <nav> elements.")
        nav_elements = soup.find_all('nav')
        for nav in nav_elements:
            potential_links = nav.find_all('a', href=lambda href: href and (href.startswith('/docs') or not href.startswith(('http', '#', '/'))))
            if len(potential_links) > 5:
                logging.info(f"Found <nav> element containing {len(potential_links)} potential doc links.")
                nav_links = potential_links
                break

    if not nav_links:
         # Last resort - all links
         logging.warning("Could not find specific navigation structure. Searching all links.")
         nav_links = soup.find_all('a', href=True)

    # Filtering Logic
    found_links_count = 0
    for link in nav_links:
        href = link['href']
        absolute_url = urljoin(base_url, href)
        parsed_url = urlparse(absolute_url)

        if (parsed_url.netloc == urlparse(base_url).netloc and
            parsed_url.path.startswith('/docs') and
            not href.startswith('#')):
             path = parsed_url.path
             if path != '/docs/' and path.endswith('/'):
                 path = path.rstrip('/')
             normalized_url = urljoin(base_url, path)
             links.add(normalized_url)
             found_links_count += 1

    links.add(DOCS_ENTRY_URL.rstrip('/')) # Add root docs URL

    logging.info(f"Found {len(links)} unique documentation links after filtering.")
    if not links:
         logging.warning("Link extraction resulted in an empty set.")

    return list(links)


def scrape_page_content(url):
    """Scrapes the main content area, converts it to Markdown, and extracts the title."""
    logging.info(f"Scraping: {url}")
    html_content = fetch_html(url)
    if not html_content:
        return url, None, None # Return URL, None content, None title on fetch error

    soup = BeautifulSoup(html_content, 'html.parser')
    markdown_content = None
    page_title = "Untitled" # Default title

    # --- Find the main content element ---
    main_content_div = soup.find('div', class_=re.compile(r'pb_content'))
    if main_content_div:
        logging.debug(f"Found 'div.pb_content' on {url}.")
        target_element = main_content_div
    else:
        logging.warning(f"Could not find 'div.pb_content' on {url}. Falling back to <main> tag.")
        main_tag = soup.find('main')
        if main_tag:
            target_element = main_tag
        else:
            target_element = soup.body # Fallback to body
            if not target_element:
                 logging.error(f"Could not find suitable content element (pb_content, main, body) on {url}")
                 return url, None, None # Failed extraction

    # --- Extract Title ---
    if target_element:
        # Try getting title from the first H1 within the target element
        h1 = target_element.find('h1')
        if h1 and h1.get_text(strip=True):
            page_title = h1.get_text(strip=True)
            logging.debug(f"Extracted title from H1: '{page_title}'")
        else:
            # Fallback to the main <title> tag if no H1 in content
            title_tag = soup.find('title')
            if title_tag and title_tag.get_text(strip=True):
                page_title = title_tag.get_text(strip=True)
                # Often includes site name, try to clean it
                page_title = page_title.replace("| PocketBase", "").strip()
                logging.debug(f"Extracted title from <title> tag: '{page_title}'")
            else:
                logging.warning(f"Could not extract title for {url}")

    # --- Convert HTML to Markdown ---
    if target_element:
        # --- Cleanup ---
        elements_to_remove = [
            'nav', 'aside', 'header', 'footer', '.toc', '.page-toc', '.breadcrumbs',
            '.edit-page-link', 'button.edit-page-button', '.feedback-widget',
            'script', 'style', 'noscript', 'svg', '.next-prev-links',
            '.language-tabs > .tabs', '.code-toolbar > .toolbar',
            'div.code-toolbar > button', 'div[class*="language-"] > button',
             '.code-copy-button-container',
        ]
        # logging.debug(f"Attempting to remove clutter elements from {url}...") # Can be noisy
        removed_count = 0
        for selector in elements_to_remove:
            try:
                elements = target_element.select(selector)
                for element in elements:
                    if element and element.parent:
                        element.decompose()
                        removed_count += 1
            except Exception as e:
                logging.warning(f"Warning: Error during cleanup with selector '{selector}': {e}")
        # logging.debug(f"Removed ~{removed_count} clutter elements.")

        # Configure html2text
        h = html2text.HTML2Text()
        h.body_width = 0
        h.ul_item_mark = '*'
        h.emphasis_mark = '*'
        h.strong_mark = '**'

        try:
            html_string = str(target_element)
            markdown_content = h.handle(html_string)
            markdown_content = re.sub(r'\n{3,}', '\n\n', markdown_content).strip()
        except Exception as e:
            logging.error(f"Error during Markdown conversion for {url}: {e}")
            markdown_content = f"Error during Markdown conversion: {e}\n\n" + target_element.get_text(separator='\n', strip=True)

    if not markdown_content or len(markdown_content.strip()) < 20:
        logging.warning(f"Extracted very little or no Markdown content from {url}.")

    return url, markdown_content, page_title # Return URL, content, and title


def save_content(url, content, output_dir):
    """Saves the scraped Markdown content to a file named after the URL slug with .md extension."""
    if content is None or not content.strip():
        logging.warning(f"Skipping save for {url} due to missing or empty content.")
        return None # Return None to indicate save failure/skip

    parsed_url = urlparse(url)
    path_segment = parsed_url.path

    if path_segment.startswith('/docs/'):
        slug = path_segment[len('/docs/'):]
    elif path_segment == '/docs' or path_segment == '/docs/':
        slug = 'index'
    else:
        slug = path_segment.strip('/')
        logging.warning(f"Encountered unexpected path structure: {path_segment}. Using slug: {slug}")

    filename_base = sanitize_filename(slug if slug else "index")
    filename = f"{filename_base}.md"
    filepath = os.path.join(output_dir, filename)

    try:
        with open(filepath, 'w', encoding='utf-8') as f:
            f.write(f"# Source URL: {url}\n\n")
            f.write(content.strip())
        logging.info(f"Successfully saved: {filepath}")
        return filename # Return the generated filename on success
    except IOError as e:
        logging.error(f"Error saving file {filepath}: {e}")
        return None # Return None on failure
    except Exception as e:
        logging.error(f"An unexpected error occurred while saving {filepath}: {e}")
        return None # Return None on failure

def generate_index_file(index_data, output_dir, index_filename):
    """Generates the central index file (llms.txt)."""
    filepath = os.path.join(output_dir, index_filename)
    logging.info(f"Generating index file: {filepath}")

    if not index_data:
        logging.warning("No data collected to generate index file.")
        return

    try:
        with open(filepath, 'w', encoding='utf-8') as f:
            f.write("# PocketBase Documentation Index for LLM\n")
            f.write("# Format: filename.md: Page Title/Description\n\n")
            # Sort data by filename for consistent order
            sorted_data = sorted(index_data, key=lambda item: item['filename'])
            for item in sorted_data:
                # Basic cleaning for the title/description
                description = item['title'].replace('\n', ' ').strip()
                f.write(f"{item['filename']}: {description}\n")
        logging.info(f"Successfully generated index file with {len(index_data)} entries.")
    except IOError as e:
        logging.error(f"Error writing index file {filepath}: {e}")
    except Exception as e:
        logging.error(f"An unexpected error occurred while generating index file {filepath}: {e}")


# --- Main Execution ---
if __name__ == "__main__":
    logging.info(f"Starting PocketBase documentation scraper for {DOCS_ENTRY_URL}")
    logging.info(f"Output directory: {OUTPUT_DIR}")

    os.makedirs(OUTPUT_DIR, exist_ok=True)
    # Pre-compile regex patterns used in sanitize_filename
    sanitize_filename.pattern1 = re.compile(r'[\\/]+')
    sanitize_filename.pattern2 = re.compile(r'[^a-zA-Z0-9_\-]')

    logging.info(f"Fetching main docs page to find links: {DOCS_ENTRY_URL}")
    entry_html = fetch_html(DOCS_ENTRY_URL)

    if not entry_html:
        logging.critical("Failed to fetch the main documentation page. Exiting.")
        exit(1)

    doc_urls = get_doc_links(entry_html, BASE_URL)

    if not doc_urls:
        logging.critical("No documentation links found. Exiting.")
        exit(1)

    logging.info(f"Found {len(doc_urls)} documentation pages to scrape.")
    logging.info("Starting scraping process...")

    failed_urls = set()
    index_data = [] # <--- List to store {'filename': ..., 'title': ...}
    processed_count = 0
    total_urls = len(doc_urls)

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        future_to_url = {executor.submit(scrape_page_content, url): url for url in doc_urls}

        for future in as_completed(future_to_url):
            original_url = future_to_url[future]
            processed_count += 1
            if processed_count % 5 == 0 or processed_count == total_urls:
                logging.info(f"Processing result {processed_count}/{total_urls}...")
            try:
                # Add a small delay between requests to avoid hammering the server
                time.sleep(REQUEST_DELAY)
                scraped_url, content, title = future.result() # <--- Get title now

                if content and title: # Ensure we have content and a title to proceed
                    saved_filename = save_content(scraped_url, content, OUTPUT_DIR)
                    if saved_filename:
                        index_data.append({'filename': saved_filename, 'title': title})
                    else:
                        # Save failed, mark URL as failed if not already caught by scrape failure
                        logging.warning(f"Save failed for {scraped_url}, adding to failed list.")
                        with FAILED_URLS_LOCK:
                            failed_urls.add(original_url)
                elif content is None: # Scrape itself failed or returned no content
                    logging.warning(f"Scrape failed or returned no content for {scraped_url}, adding to failed list.")
                    with FAILED_URLS_LOCK:
                        failed_urls.add(original_url)
                else: # Content might exist, but title extraction failed - log but maybe proceed?
                    logging.warning(f"Content scraped but title missing for {scraped_url}. Saving content but skipping index entry.")
                    saved_filename = save_content(scraped_url, content, OUTPUT_DIR)
                    if not saved_filename:
                        with FAILED_URLS_LOCK:
                            failed_urls.add(original_url) # If save also fails

            except Exception as exc:
                logging.error(f"URL {original_url} generated an exception during processing future: {exc}", exc_info=True) # Log stack trace
                with FAILED_URLS_LOCK:
                    failed_urls.add(original_url)

    logging.info("Scraping process finished.")

    # --- Generate the index file ---
    generate_index_file(index_data, OUTPUT_DIR, INDEX_FILENAME)

    # --- Final Summary ---
    logging.info("--------------------")
    unique_failed_urls = sorted(list(failed_urls))
    successful_count = total_urls - len(unique_failed_urls)

    logging.info(f"Attempted to process {total_urls} pages.")
    logging.info(f"Successfully saved content for approximately {len(index_data)} pages (indexed).") # Count indexed pages
    logging.info(f"Failures encountered (fetch, scrape, or save): {len(unique_failed_urls)}")

    if unique_failed_urls:
        logging.warning("\nFailed URLs (could be fetch, scrape, or save issues):")
        for url in unique_failed_urls:
            logging.warning(f"- {url}")
    else:
        logging.info("All discovered documentation pages processed without major errors reported during scraping/saving!")
    logging.info(f"Markdown files and index file saved in: {OUTPUT_DIR}")
    logging.info("--------------------")