import feedparser
from datetime import datetime
import time
from app.database import SessionLocal, engine
from app.models import Base, NewsSource, NewsRaw

RSS_SOURCES = [
    {
        "source_id": 1,
        "code": "ria",
        "name": "РИА Новости",
        "base_url": "https://ria.ru",
        "url": "https://ria.ru/export/rss2/index.xml",
        "parser_key": "rss"
    },
    {
        "source_id": 2,
        "code": "interfax",
        "name": "Интерфакс",
        "base_url": "https://www.interfax.ru",
        "url": "https://www.interfax.ru/rss.asp",
        "parser_key": "rss"
    }
]

def parse_and_save_rss():
    Base.metadata.create_all(bind=engine)
    
    db = SessionLocal()
    print("=== Запуск сбора данных из RSS-лент ===")
    
    try:
        
        for source_data in RSS_SOURCES:
            existing_source = db.query(NewsSource).filter_by(id=source_data["source_id"]).first()
            if not existing_source:
                new_source = NewsSource(
                    id=source_data["source_id"],
                    code=source_data["code"],
                    name=source_data["name"],
                    base_url=source_data["base_url"],
                    listing_url=source_data["url"],
                    parser_key=source_data["parser_key"],
                    enabled=True
                )
                db.add(new_source)
        db.commit()

        for source in RSS_SOURCES:
            print(f"Парсинг источника: {source['name']} ({source['url']})")
            
            feed = feedparser.parse(source["url"])
            
            count_saved = 0
            for entry in feed.entries:
                title = entry.get("title", "")
                url = entry.get("link", "")
                text = entry.get("summary", entry.get("description", ""))
                
                published_parsed = entry.get("published_parsed")
                if published_parsed:
                    published_at = datetime.fromtimestamp(time.mktime(published_parsed))
                else:
                    published_at = datetime.utcnow()

                existing = db.query(NewsRaw).filter_by(url=url).first()
                if existing:
                    continue

                db_item = NewsRaw(
                    source_id=source["source_id"],
                    title=title,
                    text=text,
                    url=url,
                    published_at=published_at,
                    status="pending",
                    attempt_count=0
                )
                
                db.add(db_item)
                count_saved += 1
            
            db.commit()
            print(f"Успешно добавлено новых записей из {source['name']}: {count_saved}")
            
    except Exception as e:
        db.rollback()
        print(f"Произошла ошибка во время парсинга RSS: {e}")
    finally:
        db.close()
        print("=== Сбор данных завершен, сессия закрыта ===")

if __name__ == "__main__":
    parse_and_save_rss()