import re
import time
import unicodedata
from datetime import date, datetime
from pathlib import Path
from uuid import uuid4

import pandas as pd
import streamlit as st
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.chrome.options import Options


st.set_page_config(page_title="Método PUT + CALL", layout="wide")

ARQUIVO_OPERACOES = Path("operacoes_abertas.csv")

MESES = {
    "A": "Jan", "B": "Fev", "C": "Mar", "D": "Abr", "E": "Mai", "F": "Jun",
    "G": "Jul", "H": "Ago", "I": "Set", "J": "Out", "K": "Nov", "L": "Dez",
    "M": "Jan", "N": "Fev", "O": "Mar", "P": "Abr", "Q": "Mai", "R": "Jun",
    "S": "Jul", "T": "Ago", "U": "Set", "V": "Out", "W": "Nov", "X": "Dez",
}

VENCIMENTOS_OPCOESNET = {
    "Vencimento atual": None,
    "Jul - 17/07": "17/07",
    "Ago - 21/08": "21/08",
    "Set - 18/09": "18/09",
    "Out - 16/10": "16/10",
    "Nov - 19/11": "19/11",
    "Dez - 18/12": "18/12",
}

COLUNAS_MERCADO = [
    "ativo", "codigo", "tipo", "fm", "estilo", "strike", "situacao",
    "distancia_pct", "ultimo", "variacao_pct", "data_ultimo_negocio",
    "negocios", "volume", "volatilidade", "delta", "gamma", "theta",
    "theta_pct", "vega", "iq", "coberto", "travado", "descoberto",
    "titulares", "lancadores",
]

COLUNAS_OPERACOES = [
    "id", "data_abertura", "ativo_base", "codigo", "tipo", "strike",
    "premio_recebido", "quantidade", "vencimento", "cotacao_atual_manual",
    "status", "observacao",
]


# ----------------------------- CONVERSÕES -----------------------------
def normalizar_texto(texto):
    texto = unicodedata.normalize("NFKD", str(texto or ""))
    texto = "".join(c for c in texto if not unicodedata.combining(c))
    texto = texto.lower().replace("\n", " ")
    return re.sub(r"\s+", " ", texto).strip()


def moeda_para_float(valor):
    try:
        texto = str(valor).strip()
        if texto == "" or texto.lower() in {"nan", "none", "-"}:
            return 0.0
        texto = (
            texto.replace("R$", "")
            .replace(" ", "")
            .replace("+", "")
            .replace("−", "-")
            .replace("%", "")
        )
        # Formato brasileiro: 18.716,00 -> 18716.00
        if "," in texto:
            texto = texto.replace(".", "").replace(",", ".")
        return float(texto)
    except (TypeError, ValueError):
        return 0.0


def fmt_rs(valor):
    try:
        return f"R$ {float(valor):,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
    except (TypeError, ValueError):
        return "R$ 0,00"


def fmt_pct(valor, casas=1):
    try:
        return f"{float(valor):.{casas}f}%".replace(".", ",")
    except (TypeError, ValueError):
        return "0,0%"


def mes_opcao(codigo):
    try:
        return MESES.get(str(codigo).strip().upper()[4], "Indef.")
    except (IndexError, TypeError):
        return "Indef."


def codigo_parece_opcao(codigo, ativo):
    codigo = str(codigo or "").strip().upper()
    raiz = re.sub(r"\d+$", "", str(ativo).upper())
    return bool(re.fullmatch(rf"{re.escape(raiz)}[A-X]\d+", codigo))


# ----------------------------- SELENIUM -----------------------------
def criar_driver():
    options = Options()
    options.add_argument("--headless=new")
    options.add_argument("--window-size=1920,1080")
    options.add_argument("--disable-gpu")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-extensions")
    options.add_argument("--blink-settings=imagesEnabled=false")
    return webdriver.Chrome(options=options)


def aceitar_dados_fechamento(driver):
    textos = ["Continuar com dados de fechamento", "Continuar", "Aceitar", "OK"]
    for texto in textos:
        try:
            elementos = driver.find_elements(
                By.XPATH, f"//*[contains(normalize-space(text()), '{texto}')]"
            )
            for elemento in elementos:
                if elemento.is_displayed() and elemento.is_enabled():
                    driver.execute_script("arguments[0].click();", elemento)
                    time.sleep(1)
                    return True
        except Exception:
            continue
    return False


def localizar_checkbox_vencimento(driver, vencimento_texto):
    """Localiza o checkbox imediatamente ligado ao texto do vencimento."""
    elementos = driver.find_elements(
        By.XPATH,
        f"//*[contains(normalize-space(text()), '{vencimento_texto}')]",
    )
    for elemento in elementos:
        try:
            if not elemento.is_displayed():
                continue

            candidatos_xpath = [
                ".//input[@type='checkbox']",
                "./preceding-sibling::input[@type='checkbox'][1]",
                "./preceding::input[@type='checkbox'][1]",
                "./ancestor::label[1]//input[@type='checkbox']",
                "./parent::*//input[@type='checkbox']",
            ]
            for xpath in candidatos_xpath:
                try:
                    checkbox = elemento.find_element(By.XPATH, xpath)
                    if checkbox.is_displayed():
                        return checkbox
                except Exception:
                    continue
        except Exception:
            continue
    return None


def selecionar_vencimento(driver, vencimento_texto):
    if vencimento_texto is None:
        return True

    time.sleep(1)
    checkbox_alvo = localizar_checkbox_vencimento(driver, vencimento_texto)
    if checkbox_alvo is None:
        return False

    # Desmarca os demais vencimentos visíveis para a grade trazer só o solicitado.
    for checkbox in driver.find_elements(By.XPATH, "//input[@type='checkbox']"):
        try:
            if checkbox == checkbox_alvo or not checkbox.is_displayed():
                continue
            if checkbox.is_selected():
                driver.execute_script("arguments[0].click();", checkbox)
                time.sleep(0.3)
        except Exception:
            continue

    try:
        driver.execute_script(
            "arguments[0].scrollIntoView({block: 'center'});", checkbox_alvo
        )
        if not checkbox_alvo.is_selected():
            driver.execute_script("arguments[0].click();", checkbox_alvo)
        time.sleep(3)
        return checkbox_alvo.is_selected()
    except Exception:
        return False


def nome_canonico_cabecalho(cabecalho):
    texto = normalizar_texto(cabecalho)

    # A ordem importa: termos mais específicos primeiro.
    regras = [
        ("dist", "distancia_pct"),
        ("vol. financeiro", "volume"),
        ("vol financeiro", "volume"),
        ("vol. impl", "volatilidade"),
        ("vol impl", "volatilidade"),
        ("num. de neg", "negocios"),
        ("num de neg", "negocios"),
        ("n. neg", "negocios"),
        ("data/hora", "data_ultimo_negocio"),
        ("dt / hr", "data_ultimo_negocio"),
        ("theta (%)", "theta_pct"),
        ("theta ($)", "theta"),
        ("ticker", "codigo"),
        ("opcao", "codigo"),
        ("tipo", "tipo"),
        ("f.m", "fm"),
        ("mod", "estilo"),
        ("strike", "strike"),
        ("a/i/otm", "situacao"),
        ("ai/otm", "situacao"),
        ("ultimo", "ultimo"),
        ("var", "variacao_pct"),
        ("delta", "delta"),
        ("gamma", "gamma"),
        ("theta", "theta"),
        ("vega", "vega"),
        ("iq", "iq"),
        ("coberto", "coberto"),
        ("travado", "travado"),
        ("descob", "descoberto"),
        ("tit", "titulares"),
        ("lan", "lancadores"),
    ]
    for termo, nome in regras:
        if termo in texto:
            return nome
    return None


def extrair_tabela_visivel(driver, ativo):
    """Extrai a tabela rica da página usando os cabeçalhos reais do site."""
    melhor_tabela = None
    melhor_cabecalhos = []

    for tabela in driver.find_elements(By.TAG_NAME, "table"):
        try:
            if not tabela.is_displayed():
                continue
            headers = [
                h.text.strip()
                for h in tabela.find_elements(By.XPATH, ".//thead//th | .//tr[1]/th")
            ]
            normalizados = [normalizar_texto(h) for h in headers]
            pontos = sum(
                any(chave in h for h in normalizados)
                for chave in ["ticker", "strike", "delta", "ultimo"]
            )
            if pontos >= 2 and len(headers) > len(melhor_cabecalhos):
                melhor_tabela = tabela
                melhor_cabecalhos = headers
        except Exception:
            continue

    if melhor_tabela is None:
        return pd.DataFrame(columns=COLUNAS_MERCADO)

    mapa_indices = {}
    for indice, cabecalho in enumerate(melhor_cabecalhos):
        nome = nome_canonico_cabecalho(cabecalho)
        if nome and nome not in mapa_indices.values():
            mapa_indices[indice] = nome

    linhas = []
    for row in melhor_tabela.find_elements(By.XPATH, ".//tbody/tr | .//tr"):
        celulas = row.find_elements(By.TAG_NAME, "td")
        if not celulas:
            continue
        dados = [c.text.strip() for c in celulas]

        registro = {col: "" for col in COLUNAS_MERCADO}
        registro["ativo"] = ativo
        for indice, nome in mapa_indices.items():
            if indice < len(dados):
                registro[nome] = dados[indice]

        # Fallback para a estrutura exibida na captura atual do site.
        if not registro["codigo"] and len(dados) >= 18:
            fallback = {
                0: "codigo", 1: "tipo", 2: "fm", 3: "estilo", 4: "strike",
                5: "situacao", 6: "distancia_pct", 7: "ultimo",
                8: "variacao_pct", 9: "data_ultimo_negocio", 10: "negocios",
                11: "volume", 12: "volatilidade", 13: "delta", 14: "gamma",
                15: "theta", 16: "theta_pct", 17: "vega", 18: "iq",
                19: "coberto", 20: "travado", 21: "descoberto",
                22: "titulares", 23: "lancadores",
            }
            for indice, nome in fallback.items():
                if indice < len(dados):
                    registro[nome] = dados[indice]

        if codigo_parece_opcao(registro["codigo"], ativo):
            linhas.append(registro)

    if not linhas:
        return pd.DataFrame(columns=COLUNAS_MERCADO)

    return pd.DataFrame(linhas, columns=COLUNAS_MERCADO).drop_duplicates(
        subset=["ativo", "codigo", "tipo"], keep="first"
    )


def coletar_opcoes(ativo, vencimento_escolhido):
    url = f"https://opcoes.net.br/opcoes/bovespa/{ativo}"
    driver = None
    try:
        driver = criar_driver()
        driver.get(url)
        time.sleep(5)
        aceitar_dados_fechamento(driver)

        vencimento_texto = VENCIMENTOS_OPCOESNET.get(vencimento_escolhido)
        if vencimento_texto and not selecionar_vencimento(driver, vencimento_texto):
            raise RuntimeError(f"não consegui selecionar o vencimento {vencimento_texto}")

        return extrair_tabela_visivel(driver, ativo)
    finally:
        if driver is not None:
            driver.quit()


# ----------------------------- ANÁLISE -----------------------------
def classificar_exercicio(delta):
    prob = min(abs(float(delta)) * 100, 100)
    if prob < 15:
        return prob, "Muito baixa", "🟢"
    if prob < 30:
        return prob, "Baixa", "🟢"
    if prob < 50:
        return prob, "Moderada", "🟡"
    if prob < 70:
        return prob, "Alta", "🟠"
    return prob, "Muito alta", "🔴"


def preparar(df):
    df = df.copy()
    numericas = [
        "strike", "distancia_pct", "ultimo", "variacao_pct", "negocios",
        "volume", "volatilidade", "delta", "gamma", "theta", "theta_pct",
        "vega", "iq", "coberto", "travado", "descoberto", "titulares",
        "lancadores",
    ]
    for col in numericas:
        if col not in df.columns:
            df[col] = 0.0
        df[col] = df[col].apply(moeda_para_float)

    df["codigo"] = df["codigo"].astype(str).str.upper().str.strip()
    df["tipo"] = df["tipo"].astype(str).str.upper().str.strip()
    df = df[df["tipo"].isin(["CALL", "PUT"])]
    df["mes"] = df["codigo"].apply(mes_opcao)

    df["premio_total"] = df["ultimo"] * 100
    df["preco_efetivo_put"] = df["strike"] - df["ultimo"]
    df["venda_efetiva_call"] = df["strike"] + df["ultimo"]
    df["cotacao_atual"] = df.apply(
        lambda row: row["strike"] / (1 + row["distancia_pct"] / 100)
        if row["strike"] > 0 and abs(1 + row["distancia_pct"] / 100) > 0.0001
        else 0,
        axis=1,
    )

    # Score de oportunidade: prêmio, liquidez e distância. O Delta é mostrado
    # separadamente como risco aproximado de terminar no dinheiro.
    df["retorno_premio_pct"] = df.apply(
        lambda r: (r["ultimo"] / r["strike"] * 100) if r["strike"] > 0 else 0,
        axis=1,
    )
    df["score_premio"] = df["retorno_premio_pct"].apply(
        lambda x: 4 if x >= 2 else 3 if x >= 1 else 2 if x >= 0.5 else 1
    )
    df["score_liquidez"] = df.apply(
        lambda r: 4 if r["negocios"] >= 20 or r["volume"] >= 50000
        else 3 if r["negocios"] >= 5 or r["volume"] >= 10000
        else 2 if r["negocios"] >= 1 or r["volume"] > 0
        else 1,
        axis=1,
    )
    df["score_distancia"] = df["distancia_pct"].abs().apply(
        lambda x: 4 if x >= 5 else 3 if x >= 3 else 2 if x >= 1 else 1
    )
    df["score_total"] = (
        df["score_premio"] + df["score_liquidez"] + df["score_distancia"]
    )
    df["diagnostico"] = df["score_total"].apply(
        lambda x: "Muito boa" if x >= 10 else "Boa" if x >= 8
        else "Regular" if x >= 6 else "Fraca"
    )

    exercicio = df["delta"].apply(classificar_exercicio)
    df["chance_exercicio_pct"] = exercicio.apply(lambda x: x[0])
    df["chance_exercicio"] = exercicio.apply(lambda x: x[1])
    df["chance_icone"] = exercicio.apply(lambda x: x[2])

    return df.sort_values(["ativo", "tipo", "score_total"], ascending=[True, True, False])


def badge_diag(diag):
    cores = {"Muito boa": "🟢", "Boa": "🟡", "Regular": "🟠", "Fraca": "🔴"}
    return f"{cores.get(diag, '⚪')} {diag}"


def card_diagnostico(op, tipo):
    st.markdown(f"### {tipo} · Diagnóstico")
    c1, c2, c3, c4, c5 = st.columns(5)
    c1.metric("Ativo", op["ativo"])
    c2.metric("Código", op["codigo"])
    c3.metric("Cotação da ação", fmt_rs(op["cotacao_atual"]))
    c4.metric("Strike", fmt_rs(op["strike"]))
    c5.metric("Prêmio por lote", fmt_rs(op["premio_total"]))

    c6, c7, c8, c9, c10 = st.columns(5)
    if tipo == "PUT":
        c6.metric("Preço efetivo", fmt_rs(op["preco_efetivo_put"]))
    else:
        c6.metric("Venda efetiva", fmt_rs(op["venda_efetiva_call"]))
    c7.metric("Situação", op["situacao"] or "—")
    c8.metric("Delta", f"{op['delta']:.3f}".replace(".", ","))
    c9.metric("Vol. implícita", fmt_pct(op["volatilidade"]))
    c10.metric("Negócios", int(op["negocios"]))

    st.markdown(
        f"**Chance aproximada de terminar no dinheiro:** "
        f"{op['chance_icone']} **{op['chance_exercicio']}** "
        f"({fmt_pct(op['chance_exercicio_pct'], 0)} pelo módulo do Delta)."
    )
    st.caption(
        "O Delta é uma aproximação, não uma garantia de exercício. O exercício efetivo "
        "depende do preço no vencimento e das regras da opção."
    )

    fatores = []
    if abs(op["distancia_pct"]) >= 3:
        fatores.append(f"strike a {fmt_pct(abs(op['distancia_pct']))} do preço da ação")
    else:
        fatores.append("strike próximo do preço da ação")
    if op["negocios"] >= 5:
        fatores.append("liquidez razoável na sessão")
    else:
        fatores.append("poucos negócios; atenção ao preço executável")
    if op["volatilidade"] >= 35:
        fatores.append("volatilidade implícita elevada")
    elif op["volatilidade"] > 0:
        fatores.append("volatilidade implícita moderada")

    st.info(
        f"**Qualidade da oportunidade:** {badge_diag(op['diagnostico'])}. "
        + "; ".join(fatores)
        + "."
    )


# ----------------------------- OPERAÇÕES ABERTAS -----------------------------
def render_velocimetro(percentual):
    pct = max(0.0, min(float(percentual), 100.0))
    angulo = pct * 1.8
    if pct >= 80:
        cor = "#21c55d"
        faixa = "Prêmio quase todo capturado"
    elif pct >= 40:
        cor = "#f2c94c"
        faixa = "Operação em andamento"
    else:
        cor = "#ef5350"
        faixa = "Ainda há bastante prêmio em aberto"

    st.markdown(
        f"""
        <div style="display:flex;flex-direction:column;align-items:center;margin:4px 0 12px 0;">
          <div style="position:relative;width:230px;height:115px;overflow:hidden;">
            <div style="position:absolute;width:230px;height:230px;border-radius:50%;
                        background:conic-gradient(from 270deg, {cor} 0deg {angulo}deg, #e9edf3 {angulo}deg 180deg, transparent 180deg 360deg);"></div>
            <div style="position:absolute;left:25px;top:25px;width:180px;height:180px;border-radius:50%;background:white;"></div>
            <div style="position:absolute;left:112px;bottom:0;width:6px;height:88px;background:#343a40;
                        transform-origin:bottom center;transform:rotate({-90 + angulo}deg);border-radius:4px;"></div>
            <div style="position:absolute;left:103px;bottom:-9px;width:24px;height:24px;border-radius:50%;background:#343a40;"></div>
          </div>
          <div style="font-size:30px;font-weight:700;margin-top:-2px;">{pct:.1f}%</div>
          <div style="font-size:14px;color:#667085;">{faixa}</div>
        </div>
        """,
        unsafe_allow_html=True,
    )


def carregar_operacoes():
    if not ARQUIVO_OPERACOES.exists():
        return pd.DataFrame(columns=COLUNAS_OPERACOES)
    try:
        df = pd.read_csv(ARQUIVO_OPERACOES, dtype={"codigo": str, "ativo_base": str})
        for col in COLUNAS_OPERACOES:
            if col not in df.columns:
                df[col] = "" if col in {
                    "id", "data_abertura", "ativo_base", "codigo", "tipo",
                    "vencimento", "status", "observacao",
                } else 0.0
        return df[COLUNAS_OPERACOES]
    except Exception:
        return pd.DataFrame(columns=COLUNAS_OPERACOES)


def salvar_operacoes(df):
    df.to_csv(ARQUIVO_OPERACOES, index=False)


def cotacao_mercado_codigo(codigo):
    dados = st.session_state.get("dados")
    if dados is None or dados.empty:
        return None
    achado = dados[dados["codigo"].astype(str).str.upper() == str(codigo).upper()]
    if achado.empty:
        return None
    return float(achado.iloc[0]["ultimo"])


def enriquecer_operacoes(df):
    if df.empty:
        return df.copy()

    resultado = df.copy()
    for col in ["strike", "premio_recebido", "quantidade", "cotacao_atual_manual"]:
        resultado[col] = pd.to_numeric(resultado[col], errors="coerce").fillna(0)

    def preco_atual(row):
        automatico = cotacao_mercado_codigo(row["codigo"])
        return automatico if automatico is not None else float(row["cotacao_atual_manual"])

    resultado["cotacao_atual"] = resultado.apply(preco_atual, axis=1)
    resultado["valor_recebido"] = resultado["premio_recebido"] * resultado["quantidade"]
    resultado["custo_encerramento"] = resultado["cotacao_atual"] * resultado["quantidade"]
    resultado["resultado_atual"] = resultado["valor_recebido"] - resultado["custo_encerramento"]
    resultado["capital_referencia"] = resultado["strike"] * resultado["quantidade"]
    resultado["rentabilidade_atual_pct"] = resultado.apply(
        lambda r: r["resultado_atual"] / r["capital_referencia"] * 100
        if r["capital_referencia"] > 0 else 0,
        axis=1,
    )
    resultado["rentabilidade_maxima_pct"] = resultado.apply(
        lambda r: r["valor_recebido"] / r["capital_referencia"] * 100
        if r["capital_referencia"] > 0 else 0,
        axis=1,
    )
    resultado["premio_capturado_pct"] = resultado.apply(
        lambda r: r["resultado_atual"] / r["valor_recebido"] * 100
        if r["valor_recebido"] > 0 else 0,
        axis=1,
    )

    hoje = date.today()

    def dias_restantes(valor):
        try:
            return (datetime.strptime(str(valor)[:10], "%Y-%m-%d").date() - hoje).days
        except ValueError:
            return 0

    resultado["dias_restantes"] = resultado["vencimento"].apply(dias_restantes)

    def diagnostico_saida(row):
        pct = row["premio_capturado_pct"]
        dias = row["dias_restantes"]
        if row["cotacao_atual"] <= 0:
            return "⚪ Informe ou atualize a cotação"
        if pct >= 90:
            return "🟢 Avaliar encerramento: 90% ou mais capturado"
        if pct >= 80:
            return "🟢 Pode valer encerrar: 80% ou mais capturado"
        if pct >= 60 and dias <= 7:
            return "🟡 Avaliar risco versus prêmio restante"
        if pct < 0:
            return "🔴 Posição em prejuízo: revisar o risco"
        return "🟡 Manter e acompanhar"

    resultado["sinal"] = resultado.apply(diagnostico_saida, axis=1)
    return resultado


# ----------------------------- INTERFACE -----------------------------
st.title("📈 MÉTODO PUT + CALL")
st.caption("Oportunidades de mercado + acompanhamento das operações abertas")

if "empresas" not in st.session_state:
    st.session_state.empresas = ["TIMS3", "BBSE3", "ITSA4", "PETR4"]
if "operacoes" not in st.session_state:
    st.session_state.operacoes = carregar_operacoes()

aba_oportunidades, aba_operacoes = st.tabs(["🔎 Oportunidades", "📒 Operações abertas"])

with aba_oportunidades:
    topo1, topo2 = st.columns([1, 1])
    with topo1:
        st.subheader("Resumo do dia")
        st.write(f"Empresas monitoradas: **{len(st.session_state.empresas)}**")
        nova = st.text_input("Adicionar empresa", placeholder="Ex.: CPFL3").upper().strip()
        if st.button("Adicionar empresa"):
            if not nova:
                st.warning("Digite uma empresa.")
            elif len(nova) < 5:
                st.error("Ticker incompleto. Exemplo: BBAS3.")
            elif nova in st.session_state.empresas:
                st.warning("Essa empresa já está na lista.")
            elif len(st.session_state.empresas) >= 10:
                st.error("Limite de 10 empresas.")
            else:
                st.session_state.empresas.append(nova)
                st.rerun()

    with topo2:
        st.subheader("Empresas / ações")
        for emp in list(st.session_state.empresas):
            col1, col2 = st.columns([4, 1])
            col1.write(f"**{emp}**")
            if col2.button("remover", key=f"rem_{emp}"):
                st.session_state.empresas.remove(emp)
                st.rerun()

    st.divider()
    st.subheader("Vencimento para buscar")
    vencimento_busca = st.radio(
        "Escolha o vencimento antes de atualizar o mercado:",
        list(VENCIMENTOS_OPCOESNET.keys()),
        horizontal=True,
    )
    st.caption("O app busca somente o vencimento escolhido em cada empresa.")

    if st.button("🔄 Atualizar mercado", type="primary"):
        dfs = []
        progress = st.progress(0)
        status = st.empty()
        erros = []

        for i, emp in enumerate(st.session_state.empresas):
            status.write(f"Coletando {emp} — vencimento: {vencimento_busca}...")
            try:
                df_emp = coletar_opcoes(emp, vencimento_busca)
                if not df_emp.empty:
                    dfs.append(df_emp)
                else:
                    erros.append(f"{emp}: a tabela não retornou opções.")
            except Exception as exc:
                erros.append(f"{emp}: {exc}")
            progress.progress((i + 1) / max(len(st.session_state.empresas), 1))

        status.empty()
        if dfs:
            dados = preparar(pd.concat(dfs, ignore_index=True))
            if vencimento_busca != "Vencimento atual":
                mes_nome = vencimento_busca.split(" - ")[0]
                dados = dados[dados["mes"] == mes_nome].copy()

            st.session_state.dados = dados
            st.session_state.vencimento_carregado = vencimento_busca
            if dados.empty:
                st.error(
                    "A coleta encontrou linhas, mas nenhuma correspondeu ao mês selecionado. "
                    "Não foi exibida uma confirmação falsa de atualização."
                )
            else:
                st.success(f"Mercado atualizado para: {vencimento_busca}")
        else:
            st.error("Nenhuma opção foi coletada.")

        for erro in erros:
            st.warning(erro)

    if "dados" not in st.session_state:
        st.warning("Escolha o vencimento e clique em **Atualizar mercado**.")
    else:
        df = st.session_state.dados
        st.divider()
        st.write(
            f"Vencimento carregado: **{st.session_state.get('vencimento_carregado', 'Indefinido')}**"
        )
        st.write(f"Opções analisadas: **{len(df)}**")

        if df.empty:
            st.warning("Nenhuma opção encontrada para esse vencimento.")
        else:
            # Exibe até 3 melhores opções de cada empresa, evitando que uma única
            # ação ocupe todo o ranking. Os seletores usam todas as opções coletadas.
            calls_todas = df[df["tipo"] == "CALL"].sort_values("score_total", ascending=False)
            puts_todas = df[df["tipo"] == "PUT"].sort_values("score_total", ascending=False)
            calls = calls_todas.groupby("ativo", group_keys=False).head(3)
            puts = puts_todas.groupby("ativo", group_keys=False).head(3)

            col_call, col_put = st.columns(2)
            with col_call:
                st.subheader("💜 Melhores CALLs por empresa")
                if calls.empty:
                    st.warning("Nenhuma CALL encontrada.")
                    cod_call = None
                else:
                    st.dataframe(
                        calls[[
                            "ativo", "codigo", "mes", "strike", "ultimo",
                            "retorno_premio_pct", "distancia_pct", "delta",
                            "chance_exercicio", "volatilidade", "negocios", "diagnostico",
                        ]],
                        use_container_width=True,
                        hide_index=True,
                    )
                    cod_call = st.selectbox(
                        "Selecionar CALL",
                        calls_todas["codigo"].tolist(),
                        key="sel_call",
                    )

            with col_put:
                st.subheader("🟢 Melhores PUTs por empresa")
                if puts.empty:
                    st.warning("Nenhuma PUT encontrada.")
                    cod_put = None
                else:
                    st.dataframe(
                        puts[[
                            "ativo", "codigo", "mes", "strike", "ultimo",
                            "preco_efetivo_put", "retorno_premio_pct", "distancia_pct",
                            "delta", "chance_exercicio", "volatilidade", "negocios",
                            "diagnostico",
                        ]],
                        use_container_width=True,
                        hide_index=True,
                    )
                    cod_put = st.selectbox(
                        "Selecionar PUT",
                        puts_todas["codigo"].tolist(),
                        key="sel_put",
                    )

            st.divider()
            diag_call, diag_put = st.columns(2)
            with diag_call:
                if cod_call:
                    card_diagnostico(
                        df[(df["codigo"] == cod_call) & (df["tipo"] == "CALL")].iloc[0],
                        "CALL",
                    )
            with diag_put:
                if cod_put:
                    card_diagnostico(
                        df[(df["codigo"] == cod_put) & (df["tipo"] == "PUT")].iloc[0],
                        "PUT",
                    )

with aba_operacoes:
    st.subheader("📒 Cadastro e acompanhamento")
    st.caption(
        "O resultado é estimado para uma opção vendida: prêmio recebido menos o custo atual "
        "de recompra. Não inclui corretagem, emolumentos, imposto nem diferença entre último "
        "negócio e preço efetivamente executável."
    )

    with st.expander("➕ Cadastrar nova operação", expanded=st.session_state.operacoes.empty):
        with st.form("form_nova_operacao", clear_on_submit=True):
            a1, a2, a3, a4 = st.columns(4)
            ativo_base = a1.text_input("Ativo-base", value="TIMS3").upper().strip()
            codigo = a2.text_input("Código da opção", placeholder="Ex.: TIMSG235").upper().strip()
            tipo = a3.selectbox("Tipo", ["CALL", "PUT"])
            strike = a4.number_input("Strike", min_value=0.0, step=0.01, format="%.2f")

            b1, b2, b3, b4 = st.columns(4)
            premio = b1.number_input(
                "Prêmio recebido por ação", min_value=0.0, step=0.01, format="%.2f"
            )
            quantidade = b2.number_input(
                "Quantidade de ações", min_value=100, step=100, value=100
            )
            vencimento = b3.date_input("Vencimento")
            cotacao_manual = b4.number_input(
                "Cotação atual da opção (opcional)",
                min_value=0.0,
                step=0.01,
                format="%.2f",
                help="Usada quando o código não está no último mercado carregado.",
            )
            observacao = st.text_input(
                "Observação", placeholder="Ex.: CALL coberta das 100 TIMS3"
            )
            salvar = st.form_submit_button("Salvar operação", type="primary")

            if salvar:
                if not ativo_base or not codigo:
                    st.error("Preencha o ativo-base e o código da opção.")
                else:
                    nova_linha = {
                        "id": uuid4().hex[:10],
                        "data_abertura": date.today().isoformat(),
                        "ativo_base": ativo_base,
                        "codigo": codigo,
                        "tipo": tipo,
                        "strike": strike,
                        "premio_recebido": premio,
                        "quantidade": int(quantidade),
                        "vencimento": vencimento.isoformat(),
                        "cotacao_atual_manual": cotacao_manual,
                        "status": "ABERTA",
                        "observacao": observacao,
                    }
                    st.session_state.operacoes = pd.concat(
                        [st.session_state.operacoes, pd.DataFrame([nova_linha])],
                        ignore_index=True,
                    )
                    salvar_operacoes(st.session_state.operacoes)
                    st.success("Operação cadastrada.")
                    st.rerun()

    abertas = st.session_state.operacoes[
        st.session_state.operacoes["status"] == "ABERTA"
    ].copy()

    if abertas.empty:
        st.info("Nenhuma operação aberta cadastrada.")
    else:
        operacoes_calc = enriquecer_operacoes(abertas)
        total_recebido = operacoes_calc["valor_recebido"].sum()
        resultado_total = operacoes_calc["resultado_atual"].sum()
        premio_pct_total = (
            resultado_total / total_recebido * 100 if total_recebido > 0 else 0
        )
        capital_total = operacoes_calc["capital_referencia"].sum()
        rentabilidade_total = (
            resultado_total / capital_total * 100 if capital_total > 0 else 0
        )

        m1, m2, m3, m4, m5 = st.columns(5)
        m1.metric("Operações abertas", len(operacoes_calc))
        m2.metric("Prêmios recebidos", fmt_rs(total_recebido))
        m3.metric("Resultado atual estimado", fmt_rs(resultado_total))
        m4.metric("Prêmio capturado", fmt_pct(premio_pct_total))
        m5.metric("Rentabilidade atual", fmt_pct(rentabilidade_total))

        st.divider()
        for _, op in operacoes_calc.iterrows():
            with st.container(border=True):
                st.markdown(f"### {op['tipo']} · {op['codigo']} · {op['ativo_base']}")
                c1, c2, c3, c4, c5, c6 = st.columns(6)
                c1.metric("Strike", fmt_rs(op["strike"]))
                c2.metric("Prêmio recebido", fmt_rs(op["premio_recebido"]))
                c3.metric("Cotação atual", fmt_rs(op["cotacao_atual"]))
                c4.metric("Resultado", fmt_rs(op["resultado_atual"]))
                c5.metric("Prêmio capturado", fmt_pct(op["premio_capturado_pct"]))
                c6.metric("Dias restantes", int(op["dias_restantes"]))

                r1, r2, r3 = st.columns(3)
                r1.metric("Rentabilidade atual", fmt_pct(op["rentabilidade_atual_pct"]))
                r2.metric("Rentabilidade máxima", fmt_pct(op["rentabilidade_maxima_pct"]))
                r3.metric("Capital de referência", fmt_rs(op["capital_referencia"]))

                gauge_col, diag_col = st.columns([1, 2])
                with gauge_col:
                    render_velocimetro(op["premio_capturado_pct"])
                with diag_col:
                    lucro_maximo = op["valor_recebido"]
                    premio_restante = max(lucro_maximo - op["resultado_atual"], 0)
                    d1, d2 = st.columns(2)
                    d1.metric("Lucro máximo possível", fmt_rs(lucro_maximo))
                    d2.metric("Prêmio ainda em aberto", fmt_rs(premio_restante))
                    st.write(f"**Diagnóstico:** {op['sinal']}")

                if pd.notna(op["observacao"]) and str(op["observacao"]).strip() not in {"", "nan"}:
                    st.caption(str(op["observacao"]))

                e1, e2, e3 = st.columns([2, 2, 1])
                nova_cotacao = e1.number_input(
                    "Atualizar cotação manual",
                    min_value=0.0,
                    step=0.01,
                    value=float(op["cotacao_atual_manual"]),
                    format="%.2f",
                    key=f"cot_{op['id']}",
                )
                if e2.button("💾 Salvar cotação", key=f"salvar_cot_{op['id']}"):
                    idx = st.session_state.operacoes.index[
                        st.session_state.operacoes["id"] == op["id"]
                    ]
                    if len(idx):
                        st.session_state.operacoes.loc[
                            idx[0], "cotacao_atual_manual"
                        ] = nova_cotacao
                        salvar_operacoes(st.session_state.operacoes)
                        st.rerun()

                if e3.button("✅ Encerrar", key=f"encerrar_{op['id']}"):
                    idx = st.session_state.operacoes.index[
                        st.session_state.operacoes["id"] == op["id"]
                    ]
                    if len(idx):
                        st.session_state.operacoes.loc[idx[0], "status"] = "ENCERRADA"
                        salvar_operacoes(st.session_state.operacoes)
                        st.rerun()

                with st.expander("✏️ Editar ou excluir esta operação"):
                    with st.form(f"editar_{op['id']}"):
                        x1, x2, x3, x4 = st.columns(4)
                        edit_ativo = x1.text_input(
                            "Ativo-base", value=str(op["ativo_base"]), key=f"ea_{op['id']}"
                        ).upper().strip()
                        edit_codigo = x2.text_input(
                            "Código da opção", value=str(op["codigo"]), key=f"ec_{op['id']}"
                        ).upper().strip()
                        edit_tipo = x3.selectbox(
                            "Tipo",
                            ["CALL", "PUT"],
                            index=0 if str(op["tipo"]).upper() == "CALL" else 1,
                            key=f"et_{op['id']}",
                        )
                        edit_strike = x4.number_input(
                            "Strike",
                            min_value=0.0,
                            step=0.01,
                            value=float(op["strike"]),
                            format="%.2f",
                            key=f"es_{op['id']}",
                        )

                        y1, y2, y3, y4 = st.columns(4)
                        edit_premio = y1.number_input(
                            "Prêmio recebido por ação",
                            min_value=0.0,
                            step=0.01,
                            value=float(op["premio_recebido"]),
                            format="%.2f",
                            key=f"ep_{op['id']}",
                        )
                        edit_quantidade = y2.number_input(
                            "Quantidade",
                            min_value=100,
                            step=100,
                            value=int(op["quantidade"]),
                            key=f"eq_{op['id']}",
                        )
                        try:
                            data_edit = datetime.strptime(
                                str(op["vencimento"])[:10], "%Y-%m-%d"
                            ).date()
                        except ValueError:
                            data_edit = date.today()
                        edit_vencimento = y3.date_input(
                            "Vencimento", value=data_edit, key=f"ev_{op['id']}"
                        )
                        edit_cotacao = y4.number_input(
                            "Cotação manual",
                            min_value=0.0,
                            step=0.01,
                            value=float(op["cotacao_atual_manual"]),
                            format="%.2f",
                            key=f"em_{op['id']}",
                        )
                        edit_obs = st.text_input(
                            "Observação",
                            value="" if pd.isna(op["observacao"]) else str(op["observacao"]),
                            key=f"eo_{op['id']}",
                        )
                        salvar_edicao = st.form_submit_button("💾 Salvar alterações")

                        if salvar_edicao:
                            idx = st.session_state.operacoes.index[
                                st.session_state.operacoes["id"] == op["id"]
                            ]
                            if len(idx):
                                st.session_state.operacoes.loc[idx[0], [
                                    "ativo_base", "codigo", "tipo", "strike",
                                    "premio_recebido", "quantidade", "vencimento",
                                    "cotacao_atual_manual", "observacao",
                                ]] = [
                                    edit_ativo, edit_codigo, edit_tipo, edit_strike,
                                    edit_premio, int(edit_quantidade), edit_vencimento.isoformat(),
                                    edit_cotacao, edit_obs,
                                ]
                                salvar_operacoes(st.session_state.operacoes)
                                st.success("Alterações salvas.")
                                st.rerun()

                    if st.button("🗑️ Excluir definitivamente", key=f"excluir_{op['id']}"):
                        st.session_state.operacoes = st.session_state.operacoes[
                            st.session_state.operacoes["id"] != op["id"]
                        ].reset_index(drop=True)
                        salvar_operacoes(st.session_state.operacoes)
                        st.rerun()
