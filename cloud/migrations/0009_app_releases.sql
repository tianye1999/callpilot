CREATE TABLE app_releases (
  product TEXT NOT NULL,
  platform TEXT NOT NULL,
  version TEXT NOT NULL,
  published_at INTEGER NOT NULL,
  notes_zh TEXT NOT NULL,
  notes_en TEXT NOT NULL,
  download_url TEXT NOT NULL CHECK (download_url GLOB 'https://?*'),
  created_at INTEGER NOT NULL,
  PRIMARY KEY (product, platform, version)
);

CREATE INDEX app_releases_product_platform_published
  ON app_releases(product, platform, published_at DESC);
