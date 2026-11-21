"""Настройки по умолчанию для автономной версии (без ключей).

Файл создаётся скриптом sync_python.sh из ship_tracker/data/config.json:
ключи и токены здесь всегда пустые.
"""

CONFIG = {
  "api_key": "",
  "openwaters_token": "",
  "use_openwaters": true,
  "use_snapshot": true,
  "snapshot_minutes": 1.0,
  "position_interval_seconds": 15.0,
  "snapshot_max_age_hours": 6,
  "use_mmsi_watch": false,
  "mmsi_watch_connections": 1,
  "ais_connection_limit": 3,
  "mmsi_refresh_minutes": 10,
  "use_area_feed": true,
  "sea_fresh_hours": 12,
  "port_watch_km": 400.0,
  "area_radius_km": 900.0,
  "far_boxes": [
    [
      [
        35.0,
        22.0
      ],
      [
        42.0,
        30.0
      ]
    ],
    [
      [
        30.0,
        25.0
      ],
      [
        38.0,
        36.0
      ]
    ],
    [
      [
        30.0,
        10.0
      ],
      [
        38.0,
        25.0
      ]
    ]
  ],
  "use_history": true,
  "use_vf": false,
  "use_vfa": false,
  "use_mt": false,
  "use_mst": false,
  "use_dt": false,
  "use_aci": true,
  "aci_minutes": 60,
  "expected_fresh_hours": 6,
  "use_gor": true,
  "gor_minutes": 15,
  "use_mlive": true,
  "mlive_minutes": 15,
  "use_srd": true,
  "srd_minutes": 15,
  "use_sfp": true,
  "sfp_minutes": 5,
  "sfp_limit": 12,
  "sfp_stale_minutes": 5,
  "use_mo": true,
  "mo_minutes": 30,
  "use_sa": true,
  "sa_minutes": 40,
  "use_mlink": true,
  "use_sn": false,
  "sn_key": "",
  "sn_minutes": 60,
  "failover_minutes": 8,
  "failover_poll_minutes": 3,
  "failover_limit": 40,
  "use_gfw": true,
  "gfw_key": "",
  "gfw_minutes": 60,
  "gfw_days": 7,
  "gfw_max_vessels": 60,
  "use_dat": false,
  "dat_key": "",
  "dat_minutes": 600,
  "dat_max_vessels": 10,
  "use_aishub": false,
  "aishub_key": "",
  "aishub_minutes": 600,
  "vf_key": "",
  "mt_key": "",
  "mst_key": "",
  "mst_minutes": 120,
  "mst_daily_coins": 150,
  "mst_max_vessels": 40,
  "vfa_minutes": 720,
  "vfa_lookahead_minutes": 720,
  "vfa_max_records": 40,
  "vfa_daily_coins": 300,
  "bounding_boxes": [
    [
      [
        40.5,
        26.0
      ],
      [
        47.5,
        42.0
      ]
    ]
  ],
  "message_types": [
    "PositionReport",
    "ExtendedClassBPositionReport",
    "ShipStaticData",
    "StandardClassBPositionReport",
    "StaticDataReport",
    "LongRangeAisBroadcastMessage"
  ],
  "ports": [
    {
      "name": "IZMAIL",
      "label": "IZMAIL",
      "lat": 45.3517,
      "lon": 28.8367,
      "radius_km": 5.0,
      "locode": "UAIZM"
    },
    {
      "name": "VYLKOVE",
      "label": "VYLKOVE",
      "lat": 45.3975,
      "lon": 29.5853,
      "radius_km": 4.0,
      "locode": "UAVYL"
    },
    {
      "name": "ZATOKA",
      "label": "ZATOKA",
      "lat": 46.0672,
      "lon": 30.4569,
      "radius_km": 4.0,
      "locode": "UABGD"
    },
    {
      "name": "CHORNOMORSK",
      "label": "CHORNOMORSK",
      "lat": 46.3,
      "lon": 30.6667,
      "radius_km": 8.0,
      "locode": "UAILK"
    },
    {
      "name": "ODESSA",
      "label": "ODESSA",
      "lat": 46.4825,
      "lon": 30.7233,
      "radius_km": 8.0,
      "locode": "UAODS"
    },
    {
      "name": "YUZHNY",
      "label": "YUZHNY",
      "lat": 46.6233,
      "lon": 31.0167,
      "radius_km": 8.0,
      "locode": "UAYUZ"
    },
    {
      "name": "OCHAKIV",
      "label": "OCHAKIV",
      "lat": 46.6216,
      "lon": 31.5725,
      "radius_km": 6.0,
      "locode": "UAOCH"
    }
  ],
  "filter_destinations": [
    "IZMAIL",
    "VYLKOVE",
    "ZATOKA",
    "CHORNOMORSK",
    "ODESSA",
    "YUZHNY",
    "OCHAKIV"
  ],
  "filter_origins": [
    "IZMAIL",
    "VYLKOVE",
    "ZATOKA",
    "CHORNOMORSK",
    "ODESSA",
    "YUZHNY",
    "OCHAKIV"
  ],
  "trip_window_hours": 96.0,
  "min_speed_knots": 2.0,
  "alert_cooldown_hours": 6.0,
  "stale_minutes": 15.0,
  "hide_after_hours": 20,
  "bulk_refresh_enabled": true,
  "bulk_refresh_workers": 6,
  "bulk_refresh_batch": 200,
  "bulk_refresh_pause_seconds": 5,
  "vesselapi_key": "",
  "stale_lookup_limit": 6,
  "berth_stale_hours": 12,
  "berth_watch_km": 20,
  "approach_radius_km": 300,
  "approach_angle_deg": 25,
  "approach_min_sog": 1.5,
  "approach_min_closing_km": 2.0,
  "approach_min_track_minutes": 15,
  "approach_confirm_hits": 2,
  "approach_confirm_minutes": 10,
  "history_days": 7.0,
  "visits_backfill_days": 4,
  "forget_vessels_hours": 24,
  "telegram_token": "",
  "telegram_chat_id": "",
  "telegram_commands": false,
  "pinned_mmsis": [
    "232041694",
    "666089403"
  ],
  "vfa_key": "",
  "photos_auto": true
}
