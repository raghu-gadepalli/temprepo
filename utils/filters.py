def format_inr(value):
    if value is None:
        return "0.00"

    value = float(value)
    s = f"{value:.2f}"
    whole, decimal = s.split(".")

    if len(whole) > 3:
        last3 = whole[-3:]
        rest = whole[:-3]
        rest = ",".join([rest[max(i-2,0):i] for i in range(len(rest),0,-2)][::-1])
        whole = rest + "," + last3

    return f"{whole}.{decimal}"