use std::env;

#[derive(Clone, Copy)]
enum Action {
    Show,
    Hide,
    Toggle,
}
#[derive(Clone, Copy)]
enum Observed {
    Absent,
    Hidden,
    Here,
    Elsewhere,
    Ambiguous,
}
#[derive(Clone, Copy)]
enum Progress {
    Idle,
    UncertainShow,
    UncertainHide,
    ConfirmedShow,
    ConfirmedHide,
}

struct Request {
    action: Action,
    observed: Observed,
    progress: Progress,
    owned: bool,
}

fn parse(args: &[String]) -> Result<Request, &'static str> {
    if args.len() != 4 {
        return Err("invalid_request");
    }
    let action = match args[0].as_str() {
        "show" => Action::Show,
        "hide" => Action::Hide,
        "toggle" => Action::Toggle,
        _ => return Err("invalid_action"),
    };
    let observed = match args[1].as_str() {
        "absent" => Observed::Absent,
        "hidden" => Observed::Hidden,
        "here" => Observed::Here,
        "elsewhere" => Observed::Elsewhere,
        "ambiguous" => Observed::Ambiguous,
        _ => return Err("invalid_observation"),
    };
    let progress = match args[2].as_str() {
        "idle" => Progress::Idle,
        "uncertain_show" => Progress::UncertainShow,
        "uncertain_hide" => Progress::UncertainHide,
        "confirmed_show" => Progress::ConfirmedShow,
        "confirmed_hide" => Progress::ConfirmedHide,
        _ => return Err("invalid_progress"),
    };
    let owned = match args[3].as_str() {
        "verified" => true,
        "unverified" => false,
        _ => return Err("invalid_ownership"),
    };
    Ok(Request {
        action,
        observed,
        progress,
        owned,
    })
}

fn plan(r: Request) -> (&'static str, &'static str, bool) {
    if matches!(
        r.progress,
        Progress::UncertainShow | Progress::UncertainHide
    ) {
        return ("observe", "uncertain", true);
    }
    if matches!(r.observed, Observed::Ambiguous)
        || (!r.owned && !matches!(r.observed, Observed::Absent))
    {
        return ("none", "needs_attention", true);
    }
    match r.progress {
        Progress::ConfirmedShow => return ("none", "show", false),
        Progress::ConfirmedHide => return ("none", "hide", false),
        _ => (),
    }
    let show = match r.action {
        Action::Show => true,
        Action::Hide => false,
        Action::Toggle => !matches!(r.observed, Observed::Here),
    };
    match (show, r.observed) {
        (true, Observed::Absent) => ("spawn", "show", true),
        (true, Observed::Hidden | Observed::Elsewhere) => ("move", "show", true),
        (true, Observed::Here) => ("none", "show", false),
        (false, Observed::Here | Observed::Elsewhere) => ("park", "hide", true),
        (false, _) => ("none", "hide", false),
        _ => unreachable!(),
    }
}

fn main() {
    match parse(&env::args().skip(1).collect::<Vec<_>>()) {
        Ok(r) => {
            let (effect, outcome, retain_guard) = plan(r);
            println!("{{\"effect\":\"{effect}\",\"outcome\":\"{outcome}\",\"retain_guard\":{retain_guard}}}");
        }
        Err(code) => {
            println!("{{\"error\":\"{code}\"}}");
            std::process::exit(2);
        }
    }
}
