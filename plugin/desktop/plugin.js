/**
 * Live artifacts: Hermes Desktop half. Plain ESM, no build step; imports only the SDK, react and the JSX runtime.
 *
 * Talks to the same backend as the dashboard tab (dashboard/plugin_api.py) through `ctx.rest`. Contributes a
 * `/live-artifacts` page (the core `/artifacts` route is reserved and silently filtered out), a sidebar row, palette
 * commands and an inline `::live-artifact{id="..."}` transcript directive.
 *
 * Desktop guest frames get no popups, modals or window.open, so the chrome around every preview carries
 * Open in browser / Copy link buttons that go through the host (`ctx.os`).
 */

import {
  Badge,
  Button,
  ConfirmDialog,
  PALETTE_AREA,
  PanelAction,
  PanelBlock,
  PanelBody,
  PanelDetail,
  PanelEmpty,
  PanelHeader,
  PanelList,
  PanelListRow,
  PanelMeta,
  PanelPill,
  PanelSectionLabel,
  ROUTES_AREA,
  SIDEBAR_NAV_AREA,
  SandboxedFrame,
  SegmentedControl,
  TRANSCRIPT_DIRECTIVE_AREA,
  cn,
  host
} from '@hermes/plugin-sdk'
import { useCallback, useEffect, useRef, useState } from 'react'
import { jsx, jsxs } from 'react/jsx-runtime'

const ROUTE = '/live-artifacts'
const POLL_MS = 2000
// Matches the ids the store mints (token_urlsafe, plus legacy 12-char ids). The directive's attributes are
// untrusted model output, so the id is validated before it reaches a URL path.
const ID_RE = /^[A-Za-z0-9_-]{8,64}$/
// The desktop frame allowlist has no modals or popups; these are the tokens it accepts that the page can use.
const FRAME_SANDBOX = 'allow-scripts allow-forms allow-downloads'
const FILTERS = [
  { id: 'all', label: 'All' },
  { id: 'active', label: 'Active' },
  { id: 'archived', label: 'Archived' }
]

const CONFIRMS = {
  rotate: {
    title: 'Rotate link?',
    description:
      'The old link, its page cookie and every open session of this artifact stop working. Open pages fail with 403 until reloaded from the new link.',
    confirmLabel: 'Rotate'
  },
  archive: {
    title: 'Archive artifact?',
    description: 'The page stops serving and its link is revoked. Data is kept.',
    confirmLabel: 'Archive'
  },
  delete: {
    title: 'Delete artifact permanently?',
    description: 'The artifact and all of its documents are removed. This cannot be undone.',
    confirmLabel: 'Delete',
    destructive: true
  }
}

// The server stores epoch seconds.
function formatAgo(sec) {
  if (!sec) {
    return '—'
  }

  const delta = Math.max(0, Date.now() / 1000 - sec)

  if (delta < 60) {
    return 'just now'
  }

  for (const [size, unit] of [
    [86400, 'd'],
    [3600, 'h'],
    [60, 'm']
  ]) {
    if (delta >= size) {
      return `${Math.floor(delta / size)}${unit} ago`
    }
  }

  return '—'
}

function errorText(e) {
  return e instanceof Error ? e.message : String(e)
}

function el(type, props, ...children) {
  return jsxs(type, { ...props, children })
}

function Documents({ docs }) {
  const paths = Object.keys(docs).sort()

  if (!paths.length) {
    return jsx(PanelEmpty, { icon: 'database', title: 'No documents yet.' })
  }

  return jsx('div', {
    className: 'space-y-3',
    children: paths.map(path =>
      jsxs(
        'details',
        {
          className: 'text-xs',
          children: [
            jsx('summary', { className: 'cursor-pointer font-mono text-foreground/85', children: path }),
            jsx(PanelBlock, { children: JSON.stringify(docs[path], null, 2) })
          ]
        },
        path
      )
    )
  })
}

function Preview({ link, nonce, title, className }) {
  return jsx(SandboxedFrame, {
    key: link + nonce,
    className: cn('rounded border border-(--ui-border) bg-background', className),
    sandbox: FRAME_SANDBOX,
    src: link,
    title
  })
}

function Page({ ctx }) {
  const [filter, setFilter] = useState('all')
  const [query, setQuery] = useState('')
  const [list, setList] = useState(null)
  const [rotationAvailable, setRotationAvailable] = useState(false)
  const [selected, setSelected] = useState(null)
  const [detail, setDetail] = useState(null)
  const [view, setView] = useState('preview')
  const [nonce, setNonce] = useState(0)
  const [error, setError] = useState(null)
  const [confirm, setConfirm] = useState(null)
  const seq = useRef(null)

  const refresh = useCallback(async () => {
    try {
      const res = await ctx.rest(filter === 'all' ? '/artifacts' : `/artifacts?status=${filter}`)

      setList(res.artifacts)
      setRotationAvailable(res.rotation_available)
      seq.current = res.change_seq

      if (selected) {
        setDetail(await ctx.rest(`/artifacts/${encodeURIComponent(selected)}`))
      }

      setError(null)
    } catch (e) {
      setError(errorText(e))
    }
  }, [ctx, filter, selected])

  useEffect(() => {
    void refresh()
  }, [refresh])

  // The store bumps change_seq on every write, including the live artifact's own autosaves.
  useEffect(() => {
    const timer = setInterval(async () => {
      try {
        const { change_seq } = await ctx.rest('/seq')

        if (seq.current !== null && change_seq !== seq.current) {
          await refresh()
        }
      } catch (e) {
        setError(errorText(e))
      }
    }, POLL_MS)

    return () => clearInterval(timer)
  }, [ctx, refresh])

  const current = detail && selected && detail.artifact.id === selected ? detail : null

  const visible = (list ?? []).filter(a => !query || a.title.toLowerCase().includes(query.toLowerCase()))

  const act = async (label, run) => {
    try {
      await run()
      host.notify({ kind: 'success', title: label })
      await refresh()
    } catch (e) {
      host.notifyError(e, label)
    }
  }

  const copyLink = link =>
    act('Link copied', async () => {
      if (!(await ctx.os.writeClipboard(link))) {
        throw new Error('clipboard unavailable')
      }
    })

  const openLink = link =>
    act('Opened in browser', async () => {
      if (!(await ctx.os.openExternal(link))) {
        throw new Error('could not open the browser')
      }
    })

  const post = (id, action) => ctx.rest(`/artifacts/${encodeURIComponent(id)}/${action}`, { method: 'POST' })

  const runConfirmed = async () => {
    const { kind, a } = confirm

    if (kind === 'rotate') {
      await post(a.id, 'rotate')
    } else if (kind === 'archive') {
      await post(a.id, 'archive')
    } else {
      await ctx.rest(`/artifacts/${encodeURIComponent(a.id)}`, { method: 'DELETE' })
      setSelected(null)
      setDetail(null)
    }

    await refresh()
  }

  const rowMenu = a => {
    const items = [
      {
        label: a.pinned ? 'Unpin' : 'Pin',
        disabled: a.status === 'archived',
        onSelect: () =>
          act(a.pinned ? 'Unpinned' : 'Pinned', () =>
            ctx.rest(`/artifacts/${encodeURIComponent(a.id)}/pin`, { method: 'POST', body: { pinned: !a.pinned } })
          )
      }
    ]

    if (a.status !== 'archived') {
      if (rotationAvailable) {
        items.push({ label: 'Rotate link', onSelect: () => setConfirm({ kind: 'rotate', a }) })
      }

      items.push({ label: 'Archive', onSelect: () => setConfirm({ kind: 'archive', a }) })
    }

    items.push({ label: 'Delete', tone: 'danger', onSelect: () => setConfirm({ kind: 'delete', a }) })

    return items
  }

  let content
  const a = current?.artifact

  if (!selected) {
    content = jsx(PanelEmpty, { icon: 'preview', title: 'Select an artifact.' })
  } else if (!current) {
    content = jsx(PanelEmpty, { icon: 'loading~spin', title: 'Loading…' })
  } else {
    const live = current.link && a.status !== 'archived'

    content = el(
      'div',
      { className: 'flex h-full min-h-0 flex-col gap-3' },
      jsx(PanelHeader, {
        title: a.title,
        subtitle: `${a.id} · updated ${formatAgo(a.updated_at)}`,
        actions: el(
          'div',
          { className: 'flex items-center gap-1.5' },
          jsx(PanelPill, { tone: live ? 'good' : 'muted', children: a.status }),
          live && jsx(PanelAction, { icon: 'link-external', onClick: () => openLink(current.link), children: 'Open in browser' }),
          live && jsx(PanelAction, { icon: 'copy', onClick: () => copyLink(current.link), children: 'Copy link' }),
          live && view === 'preview' && jsx(PanelAction, { icon: 'refresh', onClick: () => setNonce(nonce + 1), children: 'Reload' })
        )
      }),
      el(
        'div',
        { className: 'flex items-center justify-between gap-2' },
        jsx(SegmentedControl, {
          onChange: setView,
          options: [
            { id: 'preview', label: 'Preview' },
            { id: 'docs', label: `Data (${a.doc_count})` }
          ],
          value: live ? view : 'docs'
        }),
        jsx(PanelMeta, {
          rows: [{ label: 'session', value: a.origin_session || 'none (CLI/cron)' }]
        })
      ),
      view === 'preview' && live
        ? jsx(Preview, { link: current.link, nonce, title: a.title, className: 'min-h-0 flex-1' })
        : jsx(PanelDetail, {
            children: [
              jsx(PanelSectionLabel, { children: 'Documents' }, 'label'),
              jsx(Documents, { docs: current.docs }, 'docs')
            ]
          })
    )
  }

  const cfg = confirm && CONFIRMS[confirm.kind]

  return el(
    'div',
    { className: 'flex h-full min-h-0 flex-col gap-3 p-4' },
    error && jsx('p', { className: 'text-xs text-destructive', children: error }),
    jsx(SegmentedControl, { onChange: setFilter, options: FILTERS, value: filter }),
    el(
      PanelBody,
      null,
      el(
        PanelList,
        {
          onSearchChange: setQuery,
          searchPlaceholder: 'Search artifacts',
          searchValue: query
        },
        list === null
          ? jsx(PanelEmpty, { icon: 'loading~spin', title: 'Loading…' })
          : visible.length === 0
            ? jsx(PanelEmpty, { icon: 'inbox', title: 'No artifacts.' })
            : visible.map(item =>
                jsx(
                  PanelListRow,
                  {
                    active: item.id === selected,
                    icon: item.pinned ? 'pinned' : 'preview',
                    menuItems: rowMenu(item),
                    menuLabel: 'Artifact actions',
                    meta: formatAgo(item.updated_at),
                    onSelect: () => {
                      setView('preview')
                      setSelected(item.id === selected ? null : item.id)
                    },
                    rowKey: item.id,
                    title: item.title
                  },
                  item.id
                )
              )
      ),
      jsx('div', { className: 'min-h-0 min-w-0 flex-1', children: content })
    ),
    cfg &&
      jsx(ConfirmDialog, {
        ...cfg,
        open: true,
        onClose: () => setConfirm(null),
        onConfirm: runConfirmed
      })
  )
}

// Inline card for `::live-artifact{id="..."}`. The id is untrusted model output.
function ArtifactCard({ ctx, attrs, streaming }) {
  const id = attrs.id
  const [detail, setDetail] = useState(null)
  const [error, setError] = useState(null)
  const [expanded, setExpanded] = useState(false)
  const valid = ID_RE.test(id ?? '')

  useEffect(() => {
    if (!valid || streaming) {
      return undefined
    }

    let cancelled = false

    ctx
      .rest(`/artifacts/${encodeURIComponent(id)}`)
      .then(res => !cancelled && setDetail(res))
      .catch(e => !cancelled && setError(errorText(e)))

    return () => {
      cancelled = true
    }
  }, [ctx, id, streaming, valid])

  if (!valid) {
    return jsx('p', { className: 'text-xs text-muted-foreground', children: `::live-artifact{id="${id ?? ''}"}` })
  }

  const frame = 'rounded border border-(--ui-border) bg-background'

  let body

  if (error) {
    body = jsx('p', { className: 'text-xs text-destructive', children: error })
  } else if (!detail) {
    body = jsx('p', { className: 'text-xs text-muted-foreground', children: 'Loading…' })
  } else if (!detail.link) {
    body = jsx('p', { className: 'text-xs text-muted-foreground', children: 'Archived: the page no longer serves.' })
  } else {
    body = jsx(SandboxedFrame, {
      className: frame,
      sandbox: FRAME_SANDBOX,
      src: detail.link,
      style: { height: expanded ? '36rem' : '18rem' },
      title: detail.artifact.title
    })
  }

  return el(
    'div',
    { className: 'my-2 flex flex-col gap-2 rounded-md border border-(--ui-border) p-3' },
    el(
      'div',
      { className: 'flex items-center justify-between gap-2' },
      el(
        'div',
        { className: 'min-w-0' },
        jsx('div', { className: 'truncate text-sm font-medium', children: detail?.artifact.title ?? 'Live artifact' }),
        detail && jsx('div', { className: 'text-xs text-muted-foreground', children: `updated ${formatAgo(detail.artifact.updated_at)}` })
      ),
      el(
        'div',
        { className: 'flex shrink-0 items-center gap-1.5' },
        detail?.link && jsx(Badge, { children: detail.artifact.status }),
        detail?.link &&
          jsx(Button, { size: 'xs', variant: 'ghost', onClick: () => setExpanded(!expanded), children: expanded ? 'Shrink' : 'Expand' }),
        detail?.link &&
          jsx(Button, { size: 'xs', variant: 'ghost', onClick: () => ctx.os.writeClipboard(detail.link), children: 'Copy link' }),
        detail?.link &&
          jsx(Button, { size: 'xs', onClick: () => ctx.os.openExternal(detail.link), children: 'Open in browser' }),
        jsx(Button, { size: 'xs', variant: 'ghost', onClick: () => host.navigate(ROUTE), children: 'All' })
      )
    ),
    body
  )
}

export default {
  id: 'live-artifacts',
  name: 'Live Artifacts',
  defaultEnabled: true,
  register(ctx) {
    ctx.registerMany([
      {
        id: 'page',
        area: ROUTES_AREA,
        data: { path: ROUTE },
        title: 'Live Artifacts',
        render: () => jsx(Page, { ctx })
      },
      {
        id: 'nav',
        area: SIDEBAR_NAV_AREA,
        order: 60,
        data: { codicon: 'preview', label: 'Live Artifacts', path: ROUTE }
      },
      {
        id: 'directive',
        area: TRANSCRIPT_DIRECTIVE_AREA,
        data: {
          name: 'live-artifact',
          render: ({ attrs, streaming }) => jsx(ArtifactCard, { ctx, attrs, streaming })
        }
      },
      {
        id: 'browse',
        area: PALETTE_AREA,
        data: {
          id: 'live-artifacts.browse',
          label: 'Live Artifacts: Browse',
          keywords: ['live', 'artifacts', 'pages', 'forms'],
          run: () => host.navigate(ROUTE)
        }
      }
    ])
  }
}
