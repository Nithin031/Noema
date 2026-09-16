import { useCallback, useEffect, useRef, useState } from 'react';
import { Heart, Plus, Search, X } from 'lucide-react';
import {
  memeAssetImageUrl,
  memeCenterApi,
  type CuratedResponse,
  type InterventionFeedRow,
  type MemeAsset,
  type ResponseEffectivenessRow,
  type TestDeliveryReceipt,
} from '../../api/noema';
import './meme-center.css';
import {
  MIN_SHOWS_FOR_RATE,
  RESPONSE_KINDS,
  RESPONSE_TONES,
  SENTIMENT_FILTERS,
  STATUS_FILTERS,
  describeOutcome,
  formatBytes,
  formatCooldown,
  sentimentLabel,
  truncate,
  useDebouncedValue,
} from './helpers';

const PAGE_SIZE = 60;

type EditorState = {
  open: boolean;
  assetId: string | null;
  responseId: string | null;
  kind: string;
  tone: string;
  title: string;
  body: string;
  severityMin: number;
  severityMax: number;
  cooldown: number;
  tags: string;
  enabled: boolean;
  preview: boolean;
  receipt: TestDeliveryReceipt | null;
  busy: string;
};

const emptyEditor = (assetId: string | null = null): EditorState => ({
  open: true,
  assetId,
  responseId: null,
  kind: 'MEME',
  tone: 'sarcastic',
  title: 'Bro',
  body: 'Back to {goal}.',
  severityMin: 1,
  severityMax: 5,
  cooldown: 3600,
  tags: '',
  enabled: true,
  preview: true,
  receipt: null,
  busy: '',
});

function editorFromResponse(response: CuratedResponse): EditorState {
  return {
    open: true,
    assetId: response.asset_id ?? null,
    responseId: response.id,
    kind: response.kind,
    tone: response.tone,
    title: response.title,
    body: response.body_template,
    severityMin: response.severity_min,
    severityMax: response.severity_max,
    cooldown: response.cooldown_seconds,
    tags: (response.context_tags || []).join(', '),
    enabled: response.enabled,
    preview: true,
    receipt: null,
    busy: '',
  };
}

function formatFeedTime(value?: string | null): string {
  if (!value) return '—';
  return value.slice(0, 16).replace('T', ' ');
}

function AssetDetail({
  detail,
  responses,
  effectiveness,
  feed,
  tagsDraft,
  onTagsDraft,
  tagsBusy,
  onSaveTags,
  imgBroken,
  onImgBroken,
  onToggleEnabled,
  onToggleFavorite,
  onCreateResponse,
}: {
  detail: MemeAsset;
  responses: CuratedResponse[];
  effectiveness: ResponseEffectivenessRow[];
  feed: InterventionFeedRow[];
  tagsDraft: string;
  onTagsDraft: (value: string) => void;
  tagsBusy: boolean;
  onSaveTags: () => void;
  imgBroken: boolean;
  onImgBroken: () => void;
  onToggleEnabled: (id: string, next: boolean) => void;
  onToggleFavorite: (id: string, next: boolean) => void;
  onCreateResponse: (assetId: string) => void;
}) {
  const linked = responses.filter((response) => response.asset_id === detail.id);
  const linkedIds = new Set(linked.map((response) => response.id));
  const recentUse = feed
    .filter((row) => row.meme_asset?.id === detail.id || (row.response_id != null && linkedIds.has(row.response_id)))
    .slice(0, 3);
  return (
    <div className="mc-detail">
      <div className="mc-hero">
        {imgBroken ? (
          <div className="mc-unavailable large" role="img" aria-label={`Image unavailable for ${detail.filename}`}>
            <span>Image unavailable</span>
            <small>{detail.filename}</small>
          </div>
        ) : (
          <img
            className="mc-large"
            src={memeAssetImageUrl(detail.id, false)}
            alt={truncate(detail.display_text, 200) || detail.filename || 'Meme asset full preview'}
            onError={onImgBroken}
          />
        )}
        <div className="mc-hero-cap">
          <strong>{detail.display_text ? truncate(detail.display_text, 90) : detail.filename}</strong>
          <span>{detail.enabled ? 'Active' : 'Disabled — kept for history, excluded from selection'}</span>
        </div>
      </div>
      <div>
        <dl className="mc-kv">
          <dt>Filename</dt><dd>{detail.filename}</dd>
          <dt>Source</dt><dd>{detail.source} · {detail.source_ref}</dd>
          <dt>Sentiment</dt><dd>{sentimentLabel(detail.sentiment)} <small>(dataset metadata — not a suitability verdict)</small></dd>
          <dt>OCR</dt><dd>{truncate(detail.ocr_text, 400) || '—'}</dd>
          <dt>Corrected</dt><dd>{truncate(detail.corrected_text, 400) || '—'}</dd>
          <dt>Size</dt><dd>{detail.width && detail.height ? `${detail.width}×${detail.height}` : '—'} · {formatBytes(detail.byte_size)}</dd>
          <dt>Used in</dt><dd>{detail.response_count ?? 0} response(s)</dd>
        </dl>
        <div className="mc-form">
          <label>Tags (comma separated)
            <input
              value={tagsDraft}
              onChange={(event) => onTagsDraft(event.target.value)}
              placeholder="study, exams"
              aria-label={`Tags for ${detail.filename}`}
            />
          </label>
          <div className="mc-modal-actions">
            <button className="mc-btn" disabled={tagsBusy} onClick={onSaveTags}>
              {tagsBusy ? 'Saving…' : 'Save tags'}
            </button>
          </div>
        </div>
        <div className="mc-section">
          <div className="kicker">RESPONSES USING THIS ASSET</div>
          {linked.length === 0 ? (
            <div className="mc-stat">No responses use this asset yet.</div>
          ) : (
            linked.map((response) => {
              const eff = effectiveness.find((row) => row.id === response.id);
              const shown = eff?.times_shown ?? response.times_shown;
              const rate = eff?.recovery_rate;
              return (
                <div className="mc-stat" key={response.id}>
                  <b>{response.title}</b> ({response.id}) · {response.kind} · {response.tone} · SEV {response.severity_min}–{response.severity_max} ·{' '}
                  {response.enabled ? 'enabled' : 'disabled'} · Shown <b>{shown}</b> ·{' '}
                  {shown < MIN_SHOWS_FOR_RATE || rate == null ? 'Not enough data' : `${Math.round(rate * 100)}% direct recovery`}
                </div>
              );
            })
          )}
          <div className="mc-stat">Selection reason: unavailable — Noema chooses a response first; the response may use this asset.</div>
        </div>
        <div className="mc-section">
          <div className="kicker">RECENT INTERVENTION USE</div>
          {recentUse.length === 0 ? (
            <div className="mc-stat">No interventions recorded for this asset yet.</div>
          ) : (
            recentUse.map((row) => (
              <div className="mc-stat" key={row.id}>
                {formatFeedTime(row.created_at)} · {row.delivered ? 'delivered' : 'not delivered'}
                {row.interacted ? ' · interacted' : ''} · {describeOutcome(row.outcome)}
                {row.reason ? ` · “${truncate(row.reason, 90)}”` : ''}
              </div>
            ))
          )}
        </div>
        <div className="mc-modal-actions">
          <button className="mc-btn" onClick={() => onToggleFavorite(detail.id, !detail.favorite)}>
            {detail.favorite ? '♥ Favorited' : '♡ Favorite'}
          </button>
          <button
            className="mc-btn"
            aria-pressed={detail.enabled}
            onClick={() => onToggleEnabled(detail.id, !detail.enabled)}
          >
            {detail.enabled ? 'Disable' : 'Enable'}
          </button>
          <button className="mc-btn primary" onClick={() => onCreateResponse(detail.id)}>
            <Plus size={13} aria-hidden="true" /> Create response
          </button>
        </div>
      </div>
    </div>
  );
}

export default function MemeCenterPage() {
  const [tab, setTab] = useState<'assets' | 'responses'>('assets');
  const [query, setQuery] = useState('');
  const debouncedQuery = useDebouncedValue(query);
  const [sentiment, setSentiment] = useState<string>('');
  const [status, setStatus] = useState<string>('all');
  const [assets, setAssets] = useState<MemeAsset[]>([]);
  const [total, setTotal] = useState(0);
  const [loading, setLoading] = useState(true);
  const [loadingMore, setLoadingMore] = useState(false);
  const [error, setError] = useState('');
  const [loadFailed, setLoadFailed] = useState(false);
  const [broken, setBroken] = useState<Set<string>>(new Set());
  const [notice, setNotice] = useState('');
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [detail, setDetail] = useState<MemeAsset | null>(null);
  const [editor, setEditor] = useState<EditorState>({ ...emptyEditor(), open: false });
  const [responses, setResponses] = useState<CuratedResponse[]>([]);
  const [effectiveness, setEffectiveness] = useState<ResponseEffectivenessRow[]>([]);
  const [feed, setFeed] = useState<InterventionFeedRow[]>([]);
  const [tagsDraft, setTagsDraft] = useState('');
  const [tagsBusy, setTagsBusy] = useState(false);
  const [stats, setStats] = useState<{ assets: number; favorites: number; curated_assets: number; active_responses: number } | null>(null);
  const [dropOver, setDropOver] = useState<string | null>(null);
  const sentinelRef = useRef<HTMLDivElement | null>(null);
  const offsetRef = useRef(0);

  const markBroken = useCallback((id: string) => {
    setBroken((prev) => {
      if (prev.has(id)) return prev;
      const next = new Set(prev);
      next.add(id);
      return next;
    });
  }, []);

  const loadPage = useCallback(async (reset: boolean) => {
    const offset = reset ? 0 : offsetRef.current;
    if (reset) setLoading(true);
    else setLoadingMore(true);
    setError('');
    try {
      const page = await memeCenterApi.assets({
        q: debouncedQuery || undefined,
        sentiment: sentiment || undefined,
        status,
        limit: PAGE_SIZE,
        offset,
      });
      offsetRef.current = offset + page.items.length;
      setAssets((prev) => (reset ? page.items : [...prev, ...page.items]));
      setTotal(page.total);
      if (reset) setLoadFailed(false);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Could not load assets');
      if (offset === 0) setLoadFailed(true);
    } finally {
      setLoading(false);
      setLoadingMore(false);
    }
  }, [debouncedQuery, sentiment, status]);

  useEffect(() => {
    offsetRef.current = 0;
    void loadPage(true);
  }, [loadPage]);

  useEffect(() => {
    const node = sentinelRef.current;
    if (!node) return;
    const observer = new IntersectionObserver((entries) => {
      if (entries[0]?.isIntersecting) void loadPage(false);
    }, { rootMargin: '600px' });
    observer.observe(node);
    return () => observer.disconnect();
  }, [loadPage, assets.length]);

  const refreshMeta = useCallback(async () => {
    try {
      const [statsPayload, registry, eff, feedPayload] = await Promise.all([
        memeCenterApi.stats().catch(() => null),
        memeCenterApi.registry().catch(() => ({ responses: [] as CuratedResponse[] })),
        memeCenterApi.effectiveness().catch(() => ({ effectiveness: [] as ResponseEffectivenessRow[] })),
        memeCenterApi.interventionFeed(50).catch(() => ({ interventions: [] as InterventionFeedRow[] })),
      ]);
      if (statsPayload) {
        setStats({
          assets: statsPayload.assets,
          favorites: statsPayload.favorites,
          curated_assets: statsPayload.curated_assets,
          active_responses: statsPayload.active_responses,
        });
      }
      setResponses(registry.responses || []);
      setEffectiveness(eff.effectiveness || []);
      setFeed(feedPayload.interventions || []);
    } catch { /* meta is best-effort; grid errors surface separately */ }
  }, []);

  useEffect(() => { void refreshMeta(); }, [refreshMeta]);

  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (event.key === 'Escape') {
        setDetail(null);
        setEditor((prev) => ({ ...prev, open: false }));
      }
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, []);

  const patchAsset = useCallback((id: string, patch: Partial<MemeAsset>) => {
    setAssets((prev) => prev.map((item) => (item.id === id ? { ...item, ...patch } : item)));
    setDetail((prev) => (prev && prev.id === id ? { ...prev, ...patch } : prev));
  }, []);

  const toggleFavorite = useCallback(async (id: string, next: boolean) => {
    const before = assets.find((item) => item.id === id)?.favorite ?? false;
    patchAsset(id, { favorite: next });
    try {
      await memeCenterApi.favorite(id, next);
      void refreshMeta();
    } catch (err) {
      patchAsset(id, { favorite: before });
      setError(err instanceof Error ? err.message : 'Favorite failed');
    }
  }, [assets, patchAsset, refreshMeta]);

  const toggleSelect = useCallback((id: string) => {
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  }, []);

  const bulkFavorite = useCallback(async () => {
    const ids = [...selected];
    const backup = new Map(assets.filter((a) => ids.includes(a.id)).map((a) => [a.id, a.favorite]));
    ids.forEach((id) => patchAsset(id, { favorite: true }));
    try {
      await Promise.all(ids.map((id) => memeCenterApi.favorite(id, true)));
      setSelected(new Set());
      void refreshMeta();
    } catch {
      backup.forEach((favorite, id) => patchAsset(id, { favorite }));
      setError('Bulk favorite failed — changes rolled back');
    }
  }, [selected, assets, patchAsset, refreshMeta]);

  const openDetail = useCallback(async (id: string) => {
    try {
      const payload = await memeCenterApi.asset(id);
      setDetail(payload.asset);
      setTagsDraft((payload.asset.tags || []).join(', '));
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Could not open asset');
    }
  }, []);

  const toggleAssetEnabled = useCallback(async (id: string, next: boolean) => {
    const before = assets.find((item) => item.id === id)?.enabled ?? true;
    patchAsset(id, { enabled: next });
    try {
      await memeCenterApi.setEnabled(id, next);
    } catch (err) {
      patchAsset(id, { enabled: before });
      setError(err instanceof Error ? err.message : 'Enable toggle failed');
    }
  }, [assets, patchAsset]);

  const saveAssetTags = useCallback(async () => {
    if (!detail) return;
    const tags = tagsDraft.split(',').map((t) => t.trim()).filter(Boolean);
    setTagsBusy(true);
    try {
      const payload = await memeCenterApi.tags(detail.id, tags);
      patchAsset(detail.id, { tags: payload.asset.tags });
      setDetail(payload.asset);
      setTagsDraft((payload.asset.tags || []).join(', '));
      setNotice('Tags updated');
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Tag save failed');
    } finally {
      setTagsBusy(false);
    }
  }, [detail, tagsDraft, patchAsset]);

  const saveEditor = useCallback(async () => {
    const tags = editor.tags.split(',').map((t) => t.trim()).filter(Boolean);
    const body = {
      kind: editor.kind,
      tone: editor.tone,
      title: editor.title,
      body_template: editor.body,
      severity_min: editor.severityMin,
      severity_max: editor.severityMax,
      context_tags: tags,
      cooldown_seconds: editor.cooldown,
      enabled: editor.enabled,
    };
    setEditor((prev) => ({ ...prev, busy: 'save' }));
    try {
      if (editor.responseId) {
        await memeCenterApi.updateResponse(editor.responseId, body);
        setNotice('Response updated');
      } else if (editor.assetId) {
        await memeCenterApi.createResponse({ asset_id: editor.assetId, ...body });
        setNotice('Response created — Noema can now select it');
      }
      setEditor((prev) => ({ ...prev, open: false, busy: '' }));
      void refreshMeta();
      void loadPage(true);
    } catch (err) {
      setEditor((prev) => ({ ...prev, busy: '' }));
      setError(err instanceof Error ? err.message : 'Save failed');
    }
  }, [editor, refreshMeta, loadPage]);

  const testEditorResponse = useCallback(async () => {
    if (!editor.responseId) return;
    setEditor((prev) => ({ ...prev, busy: 'test', receipt: null }));
    try {
      const receipt = await memeCenterApi.testResponse(editor.responseId, {
        goal: 'Power Electronics',
        minutes_away: 28,
      });
      setEditor((prev) => ({ ...prev, receipt, busy: '' }));
    } catch (err) {
      setEditor((prev) => ({ ...prev, busy: '' }));
      setError(err instanceof Error ? err.message : 'Test delivery failed');
    }
  }, [editor.responseId]);

  const onDropToFavorites = useCallback(async (event: React.DragEvent) => {
    event.preventDefault();
    setDropOver(null);
    const id = event.dataTransfer.getData('text/meme-asset-id');
    const ids = selected.has(id) && selected.size > 0 ? [...selected] : id ? [id] : [];
    if (!ids.length) return;
    const backup = new Map(assets.filter((a) => ids.includes(a.id)).map((a) => [a.id, a.favorite]));
    ids.forEach((itemId) => patchAsset(itemId, { favorite: true }));
    try {
      await Promise.all(ids.map((itemId) => memeCenterApi.favorite(itemId, true)));
      setSelected(new Set());
      void refreshMeta();
    } catch {
      backup.forEach((favorite, itemId) => patchAsset(itemId, { favorite }));
      setError('Drop failed — favorites rolled back');
    }
  }, [selected, assets, patchAsset, refreshMeta]);

  const onDropToResponses = useCallback((event: React.DragEvent) => {
    event.preventDefault();
    setDropOver(null);
    const id = event.dataTransfer.getData('text/meme-asset-id');
    if (id) setEditor(emptyEditor(id));
  }, []);

  const best = effectiveness
    .filter((row) => (row.times_shown ?? 0) >= 5 && row.recovery_rate != null)
    .slice()
    .sort((a, b) => (b.recovery_rate ?? 0) - (a.recovery_rate ?? 0))
    .slice(0, 5);

  const libraryDown = !loading && loadFailed && assets.length === 0;

  return (
    <div className="mc-shell">
      <div className="mc-head">
        <div className="mc-title">
          <div className="kicker">NOEMA / INTERVENTION COLLECTION</div>
          <h2>Meme Center</h2>
          <p>Curated responses Noema can use when it decides an intervention is warranted.</p>
          <p className="mc-lib">
            <span className={`mc-lib-dot${libraryDown ? ' down' : ''}`} aria-hidden="true" />
            LOCAL LIBRARY {libraryDown ? '○ UNAVAILABLE' : '● AVAILABLE'}
            <span> · browsing needs no AI key</span>
          </p>
        </div>
        <div className="mc-counts" aria-label="Collection totals">
          <span className="mc-count"><b>{stats?.assets ?? '—'}</b>assets</span>
          <span className="mc-count"><b>{stats?.curated_assets ?? '—'}</b>curated</span>
          <span className="mc-count"><b>{stats?.active_responses ?? '—'}</b>active responses</span>
          <span className="mc-count"><b>{stats?.favorites ?? '—'}</b>favorites</span>
        </div>
      </div>

      <div className="mc-tabs" role="tablist" aria-label="Meme Center views">
        <button role="tab" aria-selected={tab === 'assets'} className={tab === 'assets' ? 'active' : ''} onClick={() => setTab('assets')}>Assets</button>
        <button role="tab" aria-selected={tab === 'responses'} className={tab === 'responses' ? 'active' : ''} onClick={() => setTab('responses')}>Responses</button>
      </div>

      {error && <div className="mc-error" role="alert">{error} <button className="mc-icon-btn" onClick={() => void loadPage(true)}>retry</button><button className="mc-icon-btn" onClick={() => setError('')}>dismiss</button></div>}
      {notice && <div className="mc-notice">{notice} <button className="mc-icon-btn" onClick={() => setNotice('')}>dismiss</button></div>}

      {tab === 'assets' && (
        <>
          <div className="mc-search" role="search">
            <Search size={15} aria-hidden="true" />
            <input
              value={query}
              onChange={(event) => setQuery(event.target.value)}
              placeholder="Search memes, text, tags… (try: study, deadline, tired)"
              aria-label="Search assets by OCR text, filename, or tags"
            />
            {query && (
              <button className="mc-icon-btn" onClick={() => setQuery('')} aria-label="Clear search">
                <X size={13} aria-hidden="true" />
              </button>
            )}
          </div>

          <div className="mc-pills" aria-label="Sentiment filter">
            <span className="pill-label">SENTIMENT</span>
            {SENTIMENT_FILTERS.map((value) => (
              <button key={value || 'any'} className={sentiment === value ? 'active' : ''} onClick={() => setSentiment(value)}>
                {value ? sentimentLabel(value) : 'ALL'}
              </button>
            ))}
          </div>

          <div className="mc-pills" aria-label="Status filter">
            <span className="pill-label">STATUS</span>
            {STATUS_FILTERS.map((value) => (
              <button key={value} className={status === value ? 'active' : ''} onClick={() => setStatus(value)}>
                {value.replace(/_/g, ' ').toUpperCase()}
              </button>
            ))}
          </div>

          <div className="mc-dropzones">
            <div
              className={`mc-dropzone${dropOver === 'fav' ? ' over' : ''}`}
              onDragOver={(e) => { e.preventDefault(); setDropOver('fav'); }}
              onDragLeave={() => setDropOver(null)}
              onDrop={onDropToFavorites}
              aria-label="Drop here to favorite"
            >
              ♥ Drop here to favorite
            </div>
            <div
              className={`mc-dropzone${dropOver === 'resp' ? ' over' : ''}`}
              onDragOver={(e) => { e.preventDefault(); setDropOver('resp'); }}
              onDragLeave={() => setDropOver(null)}
              onDrop={onDropToResponses}
              aria-label="Drop here to create a response"
            >
              ＋ Drop here to create a response
            </div>
          </div>

          {selected.size > 0 && (
            <div className="mc-bulkbar">
              <span>{selected.size} selected</span>
              <button className="mc-btn" onClick={() => void bulkFavorite()}>Favorite selected</button>
              <button className="mc-btn" onClick={() => setSelected(new Set())}>Clear</button>
            </div>
          )}

          {loading && assets.length === 0 ? (
            <div className="mc-grid" aria-label="Loading collection" aria-busy="true">
              {Array.from({ length: 12 }, (_, index) => (
                <div className="mc-card mc-skel" key={index} aria-hidden="true">
                  <div className="mc-skel-img" />
                  <div className="mc-skel-lines"><i /><i /></div>
                </div>
              ))}
            </div>
          ) : loadFailed && assets.length === 0 ? (
            <div className="mc-empty" role="alert">
              Couldn't load the local asset library.
              <div className="mc-modal-actions" style={{ justifyContent: 'center' }}>
                <button className="mc-btn" onClick={() => void loadPage(true)}>Retry</button>
              </div>
            </div>
          ) : assets.length === 0 ? (
            <div className="mc-empty">
              No assets yet. {total === 0 ? 'Run ingestion first: python -m noema memes ingest --csv labels.csv --dir images' : 'Try a different search or filter.'}
            </div>
          ) : (
            <div className="mc-grid" aria-label={`${total} assets`}>
              {assets.map((asset) => {
                const title = asset.display_text ? truncate(asset.display_text, 70) : asset.filename;
                const imgBroken = broken.has(asset.id);
                return (
                <article
                  key={asset.id}
                  className={`mc-card${selected.has(asset.id) ? ' selected' : ''}${asset.enabled ? '' : ' is-disabled'}`}
                  tabIndex={0}
                  role="button"
                  aria-label={`${title}${asset.enabled ? '' : ', disabled'}`}
                  aria-pressed={selected.has(asset.id)}
                  draggable
                  onDragStart={(e) => e.dataTransfer.setData('text/meme-asset-id', asset.id)}
                  onClick={() => void openDetail(asset.id)}
                  onKeyDown={(e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); void openDetail(asset.id); } }}
                >
                  <div className="mc-thumb-wrap">
                    <label className="mc-check" aria-label={`Select ${asset.filename}`}>
                      <input
                        type="checkbox"
                        checked={selected.has(asset.id)}
                        onChange={() => toggleSelect(asset.id)}
                        onClick={(e) => e.stopPropagation()}
                        aria-label={`Select ${asset.filename}`}
                      />
                    </label>
                    <button
                      className={`mc-fav${asset.favorite ? ' on' : ''}`}
                      aria-label={asset.favorite ? `Unfavorite ${asset.filename}` : `Favorite ${asset.filename}`}
                      aria-pressed={!!asset.favorite}
                      onClick={(e) => { e.stopPropagation(); void toggleFavorite(asset.id, !asset.favorite); }}
                    >
                      <Heart size={14} aria-hidden="true" />
                    </button>
                    <div className="mc-thumb">
                      {imgBroken ? (
                        <div className="mc-unavailable" role="img" aria-label={`Image unavailable for ${asset.filename}`}>
                          <span>Image unavailable</span>
                          <small>{asset.filename}</small>
                        </div>
                      ) : (
                        <img
                          src={memeAssetImageUrl(asset.id, true)}
                          alt={title || 'Meme asset'}
                          loading="lazy"
                          draggable={false}
                          onError={() => markBroken(asset.id)}
                        />
                      )}
                    </div>
                    {!asset.enabled && <span className="mc-disabled-tag">DISABLED</span>}
                  </div>
                  <div className="mc-card-body" title={asset.filename}>
                    <div className="mc-ocr">{title ? `“${title}”` : asset.filename}</div>
                    <div className="mc-micro">
                      {(asset.response_count ?? 0) > 0 && <span>USED ×{asset.response_count}</span>}
                      {(asset.tags || []).slice(0, 2).map((tag) => (
                        <span key={tag}>#{tag}</span>
                      ))}
                    </div>
                  </div>
                </article>
                );
              })}
            </div>
          )}
          <div ref={sentinelRef} className="mc-sentinel" aria-hidden="true" />
          {loadingMore && <div className="mc-empty">Loading more…</div>}
          {!loading && assets.length > 0 && (
            <div className="mc-stat">Showing {assets.length} of {total}</div>
          )}
        </>
      )}

      {tab === 'responses' && (
        <>
          {best.length > 0 && (
            <section aria-label="Best responses">
              <div className="kicker">BEST RESPONSES · DIRECT RECOVERY ONLY</div>
              <div className="mc-best">
                {best.map((row) => (
                  <span className="mc-chip" key={row.id}>
                    <b>{row.id}</b> · {Math.round((row.recovery_rate ?? 0) * 100)}% direct recovery ({row.times_shown} shown)
                  </span>
                ))}
              </div>
            </section>
          )}
          {responses.length === 0 ? (
            <div className="mc-empty">No responses yet. Open an asset and choose “+ Response” to curate your first intervention.</div>
          ) : (
            <div className="mc-resp-grid">
              {responses.map((response) => {
                const eff = effectiveness.find((row) => row.id === response.id);
                const rate = eff?.recovery_rate;
                const shown = eff?.times_shown ?? response.times_shown;
                const linkedAsset = response.asset_id
                  ? assets.find((item) => item.id === response.asset_id) ?? null
                  : null;
                const lastUse = feed.find((row) =>
                  (row.response_id && row.response_id === response.id) ||
                  (response.asset_id && row.meme_asset?.id === response.asset_id),
                ) ?? null;
                return (
                  <article key={response.id} className={`mc-resp-card${response.enabled ? '' : ' off'}`}>
                    {response.asset_id && (
                      <img src={memeAssetImageUrl(response.asset_id, true)} alt="" loading="lazy" aria-hidden="true" />
                    )}
                    <h4>{response.title} <span className="mc-badge">{response.kind}</span></h4>
                    <p>“{truncate(response.body_template, 100)}”</p>
                    <div className="mc-meta">
                      <span className="mc-badge">{response.tone}</span>
                      <span>SEV {response.severity_min}–{response.severity_max}</span>
                      <span>COOLDOWN {formatCooldown(response.cooldown_seconds)}</span>
                      {!response.enabled && <span className="mc-badge disabled">DISABLED</span>}
                    </div>
                    {(response.context_tags || []).length > 0 && (
                      <div className="mc-stat">Tags: {(response.context_tags || []).join(', ')}</div>
                    )}
                    <div className="mc-stat">
                      Asset: {linkedAsset ? linkedAsset.filename : (response.asset_id ?? 'none')}
                      {response.last_shown_at ? ` · Last shown ${response.last_shown_at.slice(0, 10)}` : ' · Never shown'}
                    </div>
                    <div className="mc-stat">
                      Shown <b>{shown}</b> · Direct recovery <b>{eff?.recovery_count ?? response.recovery_count}</b> ·{' '}
                      {shown < MIN_SHOWS_FOR_RATE || rate == null
                        ? 'Not enough data'
                        : <b>{Math.round(rate * 100)}%</b>}
                    </div>
                    <div className="mc-stat">
                      {lastUse
                        ? `Last use ${lastUse.created_at ? lastUse.created_at.slice(0, 16).replace('T', ' ') : '—'} · ${describeOutcome(lastUse.outcome)}`
                        : 'No interventions recorded for this response yet'}
                    </div>
                    <div className="mc-card-actions">
                      <button className="mc-btn" onClick={() => setEditor(editorFromResponse(response))}>Edit</button>
                      <button
                        className="mc-icon-btn"
                        aria-pressed={response.enabled}
                        aria-label={response.enabled ? `Disable ${response.id}` : `Enable ${response.id}`}
                        onClick={() => void (async () => {
                          try {
                            await memeCenterApi.updateResponse(response.id, { enabled: !response.enabled });
                            void refreshMeta();
                          } catch (err) {
                            setError(err instanceof Error ? err.message : 'Toggle failed');
                          }
                        })()}
                      >
                        {response.enabled ? 'Disable' : 'Enable'}
                      </button>
                    </div>
                  </article>
                );
              })}
            </div>
          )}
        </>
      )}

      {detail && (
        <div className="mc-modal-backdrop" onClick={() => setDetail(null)}>
          <div
            className="mc-modal"
            role="dialog"
            aria-modal="true"
            aria-label={`Asset ${detail.filename}`}
            onClick={(e) => e.stopPropagation()}
          >
            <div className="mc-head">
              <div className="mc-title">
                <div className="kicker">ASSET DETAIL</div>
                <h2>{detail.filename}</h2>
              </div>
              <button className="mc-icon-btn" onClick={() => setDetail(null)} aria-label="Close detail">
                <X size={15} aria-hidden="true" />
              </button>
            </div>
            <AssetDetail
              detail={detail}
              responses={responses}
              effectiveness={effectiveness}
              feed={feed}
              tagsDraft={tagsDraft}
              onTagsDraft={setTagsDraft}
              tagsBusy={tagsBusy}
              onSaveTags={() => void saveAssetTags()}
              imgBroken={broken.has(detail.id)}
              onImgBroken={() => markBroken(detail.id)}
              onToggleEnabled={(id, next) => void toggleAssetEnabled(id, next)}
              onToggleFavorite={(id, next) => void toggleFavorite(id, next)}
              onCreateResponse={(assetId) => { setDetail(null); setEditor(emptyEditor(assetId)); }}
            />
          </div>
        </div>
      )}

      {editor.open && (
        <div className="mc-modal-backdrop" onClick={() => setEditor((prev) => ({ ...prev, open: false }))}>
          <div
            className="mc-modal"
            role="dialog"
            aria-modal="true"
            aria-label={editor.responseId ? 'Edit response' : 'Create response'}
            onClick={(e) => e.stopPropagation()}
          >
            <div className="mc-head">
              <div className="mc-title">
                <div className="kicker">{editor.responseId ? 'EDIT RESPONSE' : 'NEW RESPONSE'}</div>
                <h2>{editor.responseId ?? 'From selected asset'}</h2>
              </div>
              <button className="mc-icon-btn" onClick={() => setEditor((prev) => ({ ...prev, open: false }))} aria-label="Close editor">
                <X size={15} aria-hidden="true" />
              </button>
            </div>
            <div className="mc-editor">
              <div className="mc-form">
                <label>Kind
                  <select value={editor.kind} onChange={(e) => setEditor((p) => ({ ...p, kind: e.target.value }))}>
                    {RESPONSE_KINDS.map((k) => <option key={k} value={k}>{k}</option>)}
                  </select>
                </label>
                <label>Tone
                  <select value={editor.tone} onChange={(e) => setEditor((p) => ({ ...p, tone: e.target.value }))}>
                    {RESPONSE_TONES.map((t) => <option key={t} value={t}>{t}</option>)}
                  </select>
                </label>
                <label>Title
                  <input value={editor.title} onChange={(e) => setEditor((p) => ({ ...p, title: e.target.value }))} maxLength={60} />
                </label>
                <label>Copy <small>(slots: {'{goal}'}, {'{minutes_away}'}, {'{distraction}'})</small>
                  <textarea value={editor.body} rows={3} maxLength={280} onChange={(e) => setEditor((p) => ({ ...p, body: e.target.value }))} />
                </label>
                <div className="mc-form-row">
                  <label>Severity min
                    <input type="number" min={1} max={5} value={editor.severityMin} onChange={(e) => setEditor((p) => ({ ...p, severityMin: Number(e.target.value) }))} />
                  </label>
                  <label>Severity max
                    <input type="number" min={1} max={5} value={editor.severityMax} onChange={(e) => setEditor((p) => ({ ...p, severityMax: Number(e.target.value) }))} />
                  </label>
                </div>
                <div className="mc-form-row">
                  <label>Cooldown (seconds)
                    <input type="number" min={0} step={60} value={editor.cooldown} onChange={(e) => setEditor((p) => ({ ...p, cooldown: Number(e.target.value) }))} />
                  </label>
                  <label>Tags (comma separated)
                    <input value={editor.tags} onChange={(e) => setEditor((p) => ({ ...p, tags: e.target.value }))} placeholder="study, exams" />
                  </label>
                </div>
                <label><span><input type="checkbox" checked={editor.enabled} onChange={(e) => setEditor((p) => ({ ...p, enabled: e.target.checked }))} /> Enabled for selection</span></label>
                <div className="mc-modal-actions">
                  <button className="mc-btn primary" disabled={editor.busy === 'save'} onClick={() => void saveEditor()}>
                    {editor.responseId ? 'Save changes' : 'Create response'}
                  </button>
                  {editor.responseId && (
                    <button className="mc-btn" disabled={editor.busy === 'test'} onClick={() => void testEditorResponse()}>
                      Test now
                    </button>
                  )}
                </div>
                {editor.receipt && (
                  <div className="mc-receipt" role="status">
                    Test delivery: <b>{String((editor.receipt.delivery as { status?: string }).status ?? 'unknown')}</b>
                    {' '}· not persisted, counters untouched, no outcome recorded.
                  </div>
                )}
              </div>
              <div className="mc-preview-wrap">
                <span className="mc-preview-ribbon">PREVIEW · SAMPLE CONTEXT</span>
                <div className={`mc-preview-card${editor.kind === 'TEXT' || editor.kind === 'NUDGE' ? ' gentle' : ''}`}>
                  {editor.assetId && (
                    <img src={memeAssetImageUrl(editor.assetId, true)} alt="Selected response asset preview" />
                  )}
                  <h2>{editor.title || 'Noema'}</h2>
                  <p className="body">
                    {(editor.body || '').replace('{goal}', 'Power Electronics').replace('{minutes_away}', '28').replace('{distraction}', 'browsing')}
                  </p>
                  <p className="goal">Power Electronics</p>
                  <p className="context">28 min away · sample context, not real behavioral evidence</p>
                  <div className="mc-preview-actions">
                    <button type="button" className="lock" tabIndex={-1}>LOCK IN</button>
                    <button type="button" className="secondary" tabIndex={-1}>5 MIN BREAK</button>
                    <button type="button" className="quiet" tabIndex={-1}>THIS IS INTENTIONAL</button>
                  </div>
                </div>
              </div>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
