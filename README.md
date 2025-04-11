# pocketbase-llms-txt

This project contains a simple web scraper for downloading and converting the PocketBase documentation into plain text files, making it easier to use with language models and other tools.

I noticed that most LLMs struggle with PocketBase, so hopefully this helps.  

## scraper.py

`scraper.py` is a Python script that crawls the [PocketBase documentation](https://pocketbase.io/docs/), downloads each page, and saves the content as text files in the `pocketbase_docs_llm` directory. It also generates an index file (`llms.txt`) listing all the downloaded documents.

### Features

- Multi-threaded scraping for faster downloads
- Cleans and converts HTML to readable plain text
- Organizes output files by documentation section
- Generates an index file for easy reference

### Requirements

- Python 3.7+
- Install dependencies with:
  ```
  pip install -r requirements.txt
  ```

### Usage

1. Edit the configuration section at the top of `scraper.py` if you want to change the base URL or output directory.
2. Run the script:
   ```
   python scraper.py
   ```
3. The scraped documentation will be saved in the `pocketbase_docs_llm` folder.

### Notes

- Be respectful of the PocketBase website. The script includes a small delay between requests to avoid overloading the server.
- If you encounter issues, check the logs for failed URLs.

---

Enjoy using the PocketBase documentation offline or with your favorite LLM tools!
