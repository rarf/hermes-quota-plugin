# Quota Widget — Design Spec (estilo CodexBar, nativo Hermes)

> Fonte de verdade para as tasks do Kanban (board `jscrambler-test-board`).
> Inspirado no design do CodexBar (menu bar com 1 item/provedor, hover com
> % + reset countdown, merge mode, toggles por provedor, adaptive refresh) —
> implementado nativamente para o Hermes Desktop, **sem reusar o projeto CodexBar**.

## Decisões de escopo (entrevista, 12 clarificações)

1. **Barra:** 1 item por provedor se ≥2 configurados (`status != unavailable`);
   senão 1 item único "Quota".
2. **Configurado** = `status != unavailable` (qualquer quota lida, mesmo em 0%).
3. **Clique (modo ícones):** abre popover só daquele provedor.
4. **Popover:** primitiva livre (DropdownMenu / Tip), desde que mostre
   `% restante` + `reset` por window.
5. **Reset:** formato relativo curto na barra/hover (`resets in 3h 12m`);
   formato (relativo vs absoluto) **personalizável** nas configs.
6. **Configs:** dentro do pane `/quota`, com ícone de gear → sub-aba de
   configurações.
7. **Persistência:** `ctx.storage` + `useValue` (atom reativo do SDK) — barra
   reage à mudança de config.
8. **Display mode:** settings tem `auto` / `icons` / `single`
   (`auto` = regra ≥2, default). Manual sobrepõe automático.
9. **Auto-refresh default:** 10 min (respeita `fresh` do cache).
10. **Não-configurados:** nunca aparecem por padrão (barra ou pane); só se
    flag `showUnconfigured` ligada.
11. **Cache:** por perfil (`get_hermes_home()`), com **fallback global** se o
    perfil não tiver providers.
12. **Ícones:** sem logos de marca (Hermes só tem Codicon). Mantém botão/item
    atual; evolução visual só se der nativamente.

## Restrições de implementação (aprendidas com o bug)

- **Só imports de `@hermes/plugin-sdk` + `react/jsx-runtime`.**
  - `useQuery/useMutation/useQueryClient` vêm do SDK (reexport do react-query).
  - Não importar `@tanstack/react-query` direto.
  - Não importar símbolos inexistentes (`DropdownMenuTrigger` NÃO existe no
    SDK; `DropdownMenu/Content/Item/Separator` existem).
- **`ctx` vem de `register(ctx)`**, não de import.
- **Arquivo único:** `desktop-plugins/quota/plugin.js`.
  - NÃO duplicar em `profiles/<name>/desktop-plugins/quota` — o desktop carrega
    ambos e uma binding de top-level duplicada (ex: `function countdown`)
    colide no module scope → `SyntaxError: Identifier 'X' has already been
    declared` → plugin não carrega.
- **Validar antes de marcar done:** `node --check` + reload no app
  (⌘K → Reload desktop plugins) + clique abre o pane.

## Backend (já implementado, confirmar por perfil)

- `GET /api/plugins/quota/quota` → lê `$HERMES_HOME/quota_cache.json`.
- `GET /health`, `POST /refresh` (subprocess `python -m quota.quota_cache`).
- `quota_cache.py` grava em `get_hermes_home()/quota_cache.json` (já por perfil).
- **Ajuste concluído (Task t_85204632):** `api.py` GET/POST agora lê o cache do
  perfil ativo (`get_hermes_home()`) e faz fallback para o cache global
  (`~/.hermes/quota_cache.json`) quando o perfil não tiver providers. O payload
  inclui `cache_source` (path efetivo) para diagnóstico.

## Áreas de registro (SDK)

- `STATUSBAR_AREAS.right` — chip / itens por provedor.
- `ROUTES_AREA` (path `/quota`) — página full do pane.
- `PANES_AREA` — painel lateral direito.
- `SIDEBAR_NAV_AREA` — nav na sidebar (codicon `pulse`, label `Quota`).

## Tasks no Kanban (board: jscrambler-test-board)

| Task | Título |
|---|---|
| `t_faada691` | Modo de barra (auto/icons/single) + arquivo único |
| `t_18cc1b3f` | Filtro de não-configurados (showUnconfigured) |
| `t_85204632` | Auto-refresh 10min + cache por perfil (fallback global) |
| `t_e38d996e` | Hover + popover por provedor (countdown relativo) |
| `t_68d2b61c` | Tela de config (gear no pane, ctx.storage) |
| `t_e35f0e46` | Retrocompat (fallback single se 0 configurados) |
| `t_3ee4b187` | Documentar spec no vault (esta task) |
