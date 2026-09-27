from urllib.parse import urlencode
from urllib.request import Request, urlopen
import xml.etree.ElementTree as ET

base_url = "https://export.arxiv.org/api/query"
parameters = {
    "search_query": "cat:cs.AI",
    "start": 0,
    "max_results": 3,
    "sortBy": "submittedDate",
    "sortOrder": "descending",
}

url = f"{base_url}?{urlencode(parameters)}"
request = Request(url, headers={"User-Agent": "AIResearchRadar/0.1 (personal learning project)"})

with urlopen(request, timeout=20) as response:
    feed = ET.fromstring(response.read())

atom = {"atom": "http://www.w3.org/2005/Atom"}
papers = feed.findall("atom:entry", atom)

for paper in papers:
    title = paper.findtext("atom:title", default="Untitled", namespaces=atom)
    print(" ".join(title.split()))