import urllib.request
import json

res = urllib.request.urlopen("http://127.0.0.1:8085/api/v1/dashboard/snapshot").read().decode("utf-8")
print("SNAPSHOT DATA:")
print(res[:300])
