from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from datetime import date, timedelta
import warnings
warnings.filterwarnings('ignore')

creds     = Credentials.from_authorized_user_file('credentials/token.json')
yt        = build('youtube', 'v3', credentials=creds, cache_discovery=False)
analytics = build('youtubeAnalytics', 'v2', credentials=creds, cache_discovery=False)

end   = date.today().isoformat()
start = (date.today() - timedelta(days=14)).isoformat()

# --- Gunluk istatistikler ---
r = analytics.reports().query(
    ids='channel==MINE',
    startDate=start,
    endDate=end,
    metrics='views,estimatedMinutesWatched,averageViewDuration,likes,subscribersGained',
    dimensions='day',
    sort='day'
).execute()

print("=== SON 14 GUN ANALYTICS (" + start + " / " + end + ") ===")
print("{:<12} {:>10} {:>10} {:>9} {:>7} {:>7}".format("Tarih","Goruntulem","Izleme(dk)","Ort.sure","Begeni","Abone+"))
print('-'*60)
total_views = total_mins = total_subs = 0
for row in r.get('rows', []):
    d, views, mins, avg, likes, subs = row
    total_views += int(views)
    total_mins  += int(mins)
    total_subs  += int(subs)
    print("{:<12} {:>10} {:>10} {:>7}sn {:>7} {:>7}".format(d, int(views), int(mins), int(avg), int(likes), int(subs)))
print('-'*60)
print("{:<12} {:>10} {:>10} {:>16}".format("TOPLAM", total_views, total_mins, "+"+str(total_subs)+" abone"))

# --- Trafik kaynaklari ---
print()
r2 = analytics.reports().query(
    ids='channel==MINE',
    startDate=start,
    endDate=end,
    metrics='views',
    dimensions='insightTrafficSourceType',
    sort='-views'
).execute()
print("=== TRAFIK KAYNAKLARI ===")
for row in r2.get('rows', []):
    src, views = row
    print("  {:<35} {:>6} goruntulem".format(src, int(views)))

# --- En iyi videolar ---
print()
r3 = analytics.reports().query(
    ids='channel==MINE',
    startDate=start,
    endDate=end,
    metrics='views,estimatedMinutesWatched,averageViewDuration,likes',
    dimensions='video',
    sort='-views',
    maxResults=15
).execute()

video_ids = [row[0] for row in r3.get('rows', [])]
if video_ids:
    vresp = yt.videos().list(part='snippet', id=','.join(video_ids)).execute()
    titles = {v['id']: v['snippet']['title'][:45] for v in vresp.get('items', [])}

print("=== EN COK IZLENEN 15 VIDEO ===")
for row in r3.get('rows', []):
    vid, views, mins, avg, likes = row
    title = titles.get(vid, vid)
    print("  {:>5} goruntulem | {:>4}sn ort | {}".format(int(views), int(avg), title))

# --- Cihaz tipleri ---
print()
r4 = analytics.reports().query(
    ids='channel==MINE',
    startDate=start,
    endDate=end,
    metrics='views',
    dimensions='deviceType',
    sort='-views'
).execute()
print("=== CIHAZ TIPLERI ===")
for row in r4.get('rows', []):
    dev, views = row
    print("  {:<20} {:>6} goruntulem".format(dev, int(views)))
